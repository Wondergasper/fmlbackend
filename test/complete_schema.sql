-- ============================================================================
-- Farm-Connect / Farmers Market — Complete Supabase Schema
-- Run this in your Supabase SQL Editor
-- ============================================================================

-- 1. PROFILES (extends Supabase Auth)
CREATE TABLE IF NOT EXISTS profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('customer', 'vendor', 'admin', 'rider', 'driver', 'courier', 'logist')),
    phone TEXT,
    wallet_balance INTEGER DEFAULT 0,  -- stored in kobo
    status TEXT DEFAULT 'Pending Approval' CHECK (status IN ('Active', 'Suspended', 'Pending Approval')),
    display_name TEXT,
    farm_name TEXT,
    location TEXT,
    bio TEXT,
    rating NUMERIC DEFAULT 0,
    products_count INTEGER DEFAULT 0,
    bank_name TEXT,
    account_number TEXT,
    account_name TEXT,
    fulfillment_hub TEXT,
    order_cutoff TEXT,
    status_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_profiles_role ON profiles(role);
CREATE INDEX IF NOT EXISTS idx_profiles_status ON profiles(status);
CREATE INDEX IF NOT EXISTS idx_profiles_email ON profiles(email);

-- 2. PRODUCTS
CREATE TABLE IF NOT EXISTS products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price INTEGER NOT NULL,  -- in kobo
    stock INTEGER NOT NULL,
    origin TEXT NOT NULL,
    description TEXT,
    image_url TEXT,
    status TEXT DEFAULT 'Pending Approval' CHECK (status IN ('Approved', 'Pending Approval', 'Rejected')),
    status_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_products_vendor ON products(vendor_id);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_status ON products(status);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);

-- 3. ORDERS
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES profiles(id),
    total_kobo INTEGER NOT NULL,
    delivery_type TEXT DEFAULT 'standard' CHECK (delivery_type IN ('standard', 'express')),
    delivery_address TEXT NOT NULL,
    notes TEXT,
    status TEXT DEFAULT 'Processing' CHECK (status IN ('Processing', 'In Transit', 'Delivered', 'Cancelled')),
    payment_status TEXT DEFAULT 'Paid',
    status_note TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);

-- 4. ORDER_ITEMS
CREATE TABLE IF NOT EXISTS order_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_price_kobo INTEGER NOT NULL,  -- price frozen at time of order
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product ON order_items(product_id);

-- 5. WALLET_TRANSACTIONS
CREATE TABLE IF NOT EXISTS wallet_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id),
    type TEXT NOT NULL CHECK (type IN ('TopUp', 'Payout', 'Refund', 'Credit')),
    amount_kobo INTEGER NOT NULL,
    reference TEXT NOT NULL,
    status TEXT DEFAULT 'Pending' CHECK (status IN ('Success', 'Pending', 'Failed')),
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wallet_tx_user ON wallet_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_wallet_tx_reference ON wallet_transactions(reference);

-- 6. CATEGORIES
CREATE TABLE IF NOT EXISTS categories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 7. PLATFORM_CONFIG (singleton row with fixed id)
CREATE TABLE IF NOT EXISTS platform_config (
    id TEXT PRIMARY KEY DEFAULT 'platform-config',
    platform_fees INTEGER DEFAULT 5,  -- percentage
    delivery_base_fee INTEGER DEFAULT 85000,  -- kobo (850 NGN)
    delivery_express_fee INTEGER DEFAULT 150000,  -- kobo (1500 NGN)
    gateway_mode TEXT DEFAULT 'Sandbox' CHECK (gateway_mode IN ('Sandbox', 'Live')),
    signup_bonus INTEGER DEFAULT 50000,  -- kobo (500 NGN)
    farmer_rewards_rate INTEGER DEFAULT 2,  -- percentage
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed the platform_config singleton row
INSERT INTO platform_config (id) VALUES ('platform-config')
ON CONFLICT (id) DO NOTHING;

-- 8. DISPUTES
CREATE TABLE IF NOT EXISTS disputes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    created_by UUID REFERENCES profiles(id),
    opened_by UUID REFERENCES profiles(id),
    reason TEXT NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'Open' CHECK (status IN ('Open', 'Under Review', 'Resolved', 'Closed')),
    resolution_outcome VARCHAR(100) NULL CHECK (resolution_outcome IN ('Refund Buyer', 'Release to Vendor', 'Split', 'Closed', 'Dismissed')),
    resolution_notes TEXT NULL,
    resolved_by UUID NULL REFERENCES profiles(id),
    resolved_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_disputes_order ON disputes(order_id);
CREATE INDEX IF NOT EXISTS idx_disputes_created_by ON disputes(created_by);
CREATE INDEX IF NOT EXISTS idx_disputes_opened_by ON disputes(opened_by);
CREATE INDEX IF NOT EXISTS idx_disputes_status ON disputes(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_disputes_active_order ON disputes(order_id) WHERE status IN ('Open', 'Under Review');

-- 9. DISPUTE_NOTES
CREATE TABLE IF NOT EXISTS dispute_notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispute_id UUID NOT NULL REFERENCES disputes(id) ON DELETE CASCADE,
    user_id UUID REFERENCES profiles(id),
    sender_id UUID REFERENCES profiles(id),
    user_role VARCHAR(50) NOT NULL,
    sender_role VARCHAR(50) NULL,
    note_text TEXT NOT NULL,
    note TEXT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dispute_notes_dispute ON dispute_notes(dispute_id);

-- 10. DISPUTE_EVIDENCE
CREATE TABLE IF NOT EXISTS dispute_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispute_id UUID NOT NULL REFERENCES disputes(id) ON DELETE CASCADE,
    uploaded_by UUID NOT NULL REFERENCES profiles(id),
    file_path TEXT NOT NULL,
    file_url TEXT NOT NULL,
    file_type VARCHAR(50) DEFAULT 'image',
    description TEXT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dispute_evidence_dispute ON dispute_evidence(dispute_id);

-- ============================================================================
-- RPC: fn_revenue_by_period (aggregated revenue analytics)
-- ============================================================================
CREATE OR REPLACE FUNCTION fn_revenue_by_period(p_period TEXT)
RETURNS TABLE (
    period TEXT,
    total_revenue BIGINT,
    order_count BIGINT
) LANGUAGE plpgsql AS $$
BEGIN
    IF p_period = 'daily' THEN
        RETURN QUERY
        SELECT
            to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS period,
            SUM(total_kobo)::BIGINT AS total_revenue,
            COUNT(*)::BIGINT AS order_count
        FROM orders
        WHERE status IN ('Delivered', 'In Transit', 'Processing')
            AND payment_status = 'Paid'
        GROUP BY to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')
        ORDER BY period DESC;
    ELSIF p_period = 'weekly' THEN
        RETURN QUERY
        SELECT
            to_char(created_at AT TIME ZONE 'UTC', 'IYYY-"W"IW') AS period,
            SUM(total_kobo)::BIGINT AS total_revenue,
            COUNT(*)::BIGINT AS order_count
        FROM orders
        WHERE status IN ('Delivered', 'In Transit', 'Processing')
            AND payment_status = 'Paid'
        GROUP BY to_char(created_at AT TIME ZONE 'UTC', 'IYYY-"W"IW')
        ORDER BY period DESC;
    ELSIF p_period = 'monthly' THEN
        RETURN QUERY
        SELECT
            to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM') AS period,
            SUM(total_kobo)::BIGINT AS total_revenue,
            COUNT(*)::BIGINT AS order_count
        FROM orders
        WHERE status IN ('Delivered', 'In Transit', 'Processing')
            AND payment_status = 'Paid'
        GROUP BY to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM')
        ORDER BY period DESC;
    END IF;
END;
$$;

-- ============================================================================
-- STORAGE BUCKET (for product images)
-- ============================================================================
-- Run this separately if you need it:
-- INSERT INTO storage.buckets (id, name, public) VALUES ('product-images', 'product-images', true)
-- ON CONFLICT (id) DO NOTHING;
