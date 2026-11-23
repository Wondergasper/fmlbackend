-- Apply in Supabase SQL editor before enabling the new API.
BEGIN;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS account_type text NOT NULL DEFAULT 'individual';
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS business_name text;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS business_phone text;
ALTER TABLE public.profiles DROP CONSTRAINT IF EXISTS profiles_buyer_metadata_check;
ALTER TABLE public.profiles ADD CONSTRAINT profiles_buyer_metadata_check CHECK (
 (account_type = 'individual' AND business_name IS NULL AND business_phone IS NULL) OR
 (account_type = 'business' AND role = 'customer' AND length(trim(business_name)) > 0 AND business_name IS NOT NULL
  AND business_phone IS NOT NULL AND length(trim(business_phone)) >= 3));
ALTER TABLE public.products ADD COLUMN IF NOT EXISTS unit text;
ALTER TABLE public.products ADD COLUMN IF NOT EXISTS is_organic boolean;
ALTER TABLE public.products ADD COLUMN IF NOT EXISTS harvest_date date;

-- New savings tables are service-role only, including all provider identifiers.
CREATE TABLE IF NOT EXISTS public.savings_accounts (
 user_id uuid NOT NULL REFERENCES public.profiles(id),
 mode text NOT NULL CHECK (mode IN ('sandbox', 'production')),
 account_id text NOT NULL,
 wallet_id text,
 verified boolean NOT NULL DEFAULT false,
 terms_accepted boolean NOT NULL DEFAULT false,
 PRIMARY KEY (user_id, mode), UNIQUE (mode, account_id), UNIQUE (mode, wallet_id)
);
CREATE TABLE IF NOT EXISTS public.savings_operations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id uuid NOT NULL REFERENCES public.profiles(id),
 mode text NOT NULL,
 operation text NOT NULL,
 request_key text NOT NULL,
 payload_hash text NOT NULL,
 payload jsonb NOT NULL,
 provider_payload jsonb NOT NULL,
 provider_key text NOT NULL UNIQUE,
 state text NOT NULL DEFAULT 'processing' CHECK (state IN ('processing','unknown','pending','complete','failed')),
 lease_token uuid,
 lease_until timestamptz,
 provider_result jsonb,
 response jsonb,
 error_status integer,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE (user_id, mode, operation, request_key)
);
CREATE TABLE IF NOT EXISTS public.savings_goals (
 id uuid PRIMARY KEY REFERENCES public.savings_operations(id),
 user_id uuid NOT NULL REFERENCES public.profiles(id),
 mode text NOT NULL,
 name text NOT NULL,
 target_amount_kobo bigint NOT NULL CHECK (target_amount_kobo > 0 AND target_amount_kobo <= 9007199254740991),
 balance_kobo bigint NOT NULL DEFAULT 0 CHECK (balance_kobo >= 0 AND balance_kobo <= 9007199254740991),
 maturity_date date NOT NULL,
 status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','matured','completed')),
 savings_id text NOT NULL,
 product_code text NOT NULL,
 UNIQUE (mode, savings_id), UNIQUE (mode, product_code)
);
ALTER TABLE public.savings_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.savings_goals ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.savings_operations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.savings_accounts, public.savings_goals, public.savings_operations FROM anon, authenticated;
GRANT ALL ON public.savings_accounts, public.savings_goals, public.savings_operations TO service_role;

-- Unique insert plus row lock reserves an operation before any provider call.
-- The fencing token prevents a slow worker from overwriting a newer attempt.
CREATE OR REPLACE FUNCTION public.claim_savings_operation(
 p_user_id uuid, p_mode text, p_operation text, p_request_key text, p_payload_hash text,
 p_payload jsonb, p_provider_payload jsonb, p_provider_key text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE op public.savings_operations; claimed boolean := false;
BEGIN
 INSERT INTO public.savings_operations(user_id,mode,operation,request_key,payload_hash,payload,provider_payload,provider_key)
 VALUES(p_user_id,p_mode,p_operation,p_request_key,p_payload_hash,p_payload,p_provider_payload,p_provider_key)
 ON CONFLICT(user_id,mode,operation,request_key) DO NOTHING;
 SELECT * INTO op FROM public.savings_operations
 WHERE user_id=p_user_id AND mode=p_mode AND operation=p_operation AND request_key=p_request_key FOR UPDATE;
 IF op.payload_hash <> p_payload_hash THEN
  RETURN jsonb_build_object('conflict', true);
 END IF;
 IF op.state NOT IN ('complete','failed') AND (op.lease_until IS NULL OR op.lease_until < now()) THEN
  UPDATE public.savings_operations SET lease_token=gen_random_uuid(), lease_until=now()+interval '2 minutes',
    updated_at=now() WHERE id=op.id RETURNING * INTO op;
  claimed := true;
 END IF;
 RETURN jsonb_build_object('claimed',claimed,'operation',to_jsonb(op));
END; $$;
REVOKE ALL ON FUNCTION public.claim_savings_operation(uuid,text,text,text,text,jsonb,jsonb,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.claim_savings_operation(uuid,text,text,text,text,jsonb,jsonb,text) TO service_role;

-- Make public catalog reads safe even when using Supabase directly.
-- Existing authenticated policies may be broader; remove old product policies first.
DO $$ DECLARE pol record; BEGIN
 FOR pol IN SELECT policyname FROM pg_policies WHERE schemaname='public' AND tablename='products' LOOP
  EXECUTE format('DROP POLICY %I ON public.products', pol.policyname);
 END LOOP;
END $$;
ALTER TABLE public.products ENABLE ROW LEVEL SECURITY;
CREATE POLICY marketplace_public_catalog ON public.products FOR SELECT TO anon, authenticated
 USING (status='Approved' AND stock>0 AND EXISTS (
  SELECT 1 FROM public.profiles p WHERE p.id=vendor_id AND p.status='Active'));
-- All product writes and private listing reads go through the authenticated API's service client.
REVOKE INSERT, UPDATE, DELETE ON public.products FROM anon,authenticated;


ALTER TABLE public.wallet_transactions DROP CONSTRAINT IF EXISTS wallet_transactions_type_check;
ALTER TABLE public.wallet_transactions ADD CONSTRAINT wallet_transactions_type_check
CHECK (type IN ('TopUp','Payout','Refund','Credit','Debit'));

-- Checkout revalidates publication while holding product locks and commits all
-- stock, wallet, order and item changes together. No partial purchases on failure.
CREATE OR REPLACE FUNCTION public.marketplace_checkout(
 p_customer_id uuid, p_items jsonb, p_delivery_type text, p_delivery_address text, p_notes text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE buyer public.profiles; prod public.products; item record;
 order_row public.orders; subtotal bigint:=0; fee bigint; total bigint; new_balance bigint;
BEGIN
 IF p_delivery_type NOT IN ('standard','express') OR jsonb_array_length(p_items)=0 THEN
  RAISE EXCEPTION 'Invalid order';
 END IF;
 SELECT * INTO buyer FROM public.profiles WHERE id=p_customer_id FOR UPDATE;
 IF NOT FOUND OR buyer.role <> 'customer' OR buyer.status <> 'Active' THEN
  RAISE EXCEPTION 'Customer account is not active';
 END IF;
 -- Sort locks to avoid deadlocks across multi-product checkouts.
 FOR item IN SELECT (x->>'product_id')::uuid AS product_id, sum((x->>'quantity')::bigint) AS quantity
  FROM jsonb_array_elements(p_items) x GROUP BY 1 ORDER BY 1 LOOP
  IF item.quantity IS NULL OR item.quantity<=0 THEN RAISE EXCEPTION 'Invalid quantity'; END IF;
  SELECT * INTO prod FROM public.products WHERE id=item.product_id FOR UPDATE;
  IF NOT FOUND OR prod.status <> 'Approved' OR prod.stock < item.quantity OR prod.stock<=0 THEN
   RAISE EXCEPTION 'Product unavailable or insufficient stock';
  END IF;
  IF NOT EXISTS(SELECT 1 FROM public.profiles WHERE id=prod.vendor_id AND status='Active') THEN
   RAISE EXCEPTION 'Vendor is not active';
  END IF;
  subtotal := subtotal + prod.price::bigint * item.quantity;
 END LOOP;
 SELECT CASE WHEN p_delivery_type='express' THEN delivery_express_fee ELSE delivery_base_fee END
 INTO fee FROM public.platform_config WHERE id='platform-config';
 fee := coalesce(fee, CASE WHEN p_delivery_type='express' THEN 150000 ELSE 85000 END);
 IF fee < 0 THEN RAISE EXCEPTION 'Invalid delivery fee'; END IF;
 total := subtotal+fee;
 IF buyer.wallet_balance < total THEN RAISE EXCEPTION 'Insufficient wallet balance'; END IF;
 new_balance := buyer.wallet_balance-total;
 INSERT INTO public.orders(customer_id,total_kobo,delivery_type,delivery_address,notes,status,payment_status)
 VALUES(p_customer_id,total,p_delivery_type,p_delivery_address,p_notes,'Processing','Paid') RETURNING * INTO order_row;
 FOR item IN SELECT (x->>'product_id')::uuid AS product_id, sum((x->>'quantity')::bigint) AS quantity
  FROM jsonb_array_elements(p_items) x GROUP BY 1 LOOP
  INSERT INTO public.order_items(order_id,product_id,quantity,unit_price_kobo)
   SELECT order_row.id, id, item.quantity, price FROM public.products WHERE id=item.product_id;
  UPDATE public.products SET stock=stock-item.quantity WHERE id=item.product_id;
 END LOOP;
 UPDATE public.profiles SET wallet_balance=new_balance WHERE id=p_customer_id;
 INSERT INTO public.wallet_transactions(user_id,type,amount_kobo,reference,status,description)
 VALUES(p_customer_id,'Debit',total,'order_payment_'||order_row.id,'Success','Marketplace order payment');
 RETURN jsonb_build_object('order',to_jsonb(order_row),'subtotal_kobo',subtotal,'delivery_fee_kobo',fee,
   'total_kobo',total,'new_wallet_balance',new_balance);
END; $$;
REVOKE ALL ON FUNCTION public.marketplace_checkout(uuid,jsonb,text,text,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.marketplace_checkout(uuid,jsonb,text,text,text) TO service_role;

-- Browser profile updates cannot grant roles or alter wallet balances.
CREATE OR REPLACE FUNCTION public.guard_marketplace_profile() RETURNS trigger
LANGUAGE plpgsql SET search_path=public AS $$
BEGIN
 IF current_user IN ('anon','authenticated') THEN
  IF TG_OP='INSERT' THEN
   IF NEW.role <> 'customer' OR coalesce(NEW.wallet_balance,0) <> 0 THEN
    RAISE EXCEPTION 'Privileged profile creation requires the backend';
   END IF;
  ELSIF NEW.role IS DISTINCT FROM OLD.role OR NEW.wallet_balance IS DISTINCT FROM OLD.wallet_balance
     OR NEW.status IS DISTINCT FROM OLD.status THEN
   RAISE EXCEPTION 'Privileged profile fields require the backend';
  END IF;
 END IF;
 RETURN NEW;
END; $$;
DROP TRIGGER IF EXISTS guard_marketplace_profile ON public.profiles;
CREATE TRIGGER guard_marketplace_profile BEFORE INSERT OR UPDATE ON public.profiles
FOR EACH ROW EXECUTE FUNCTION public.guard_marketplace_profile();

COMMIT;
