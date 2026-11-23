# Marketplace backend setup

This repository contains the FastAPI implementation of the frontend handoff in
`BACKEND_INTEGRATION.md`. These changes have not been deployed or applied to the
live Supabase database.

## Database and configuration

1. Apply `migrations_marketplace.sql` in a staging Supabase project, using the SQL
   editor as a database administrator. It runs as a transaction. Existing core
   tables from `test/complete_schema.sql` must already exist.
2. The migration adds buyer metadata, optional product fields, private savings
   tables, atomic operation reservation, and atomic checkout. It replaces existing
   product RLS policies with public reads restricted to approved, stocked products
   from active vendors. Product writes and private lists go through the API.
   Review existing custom product policies before applying it. The profile trigger
   prevents browser updates to roles, account status and wallet balances.
3. Configure server-only `COWRYWISE_CLIENT_ID`, `COWRYWISE_CLIENT_SECRET`, and
   `COWRYWISE_MODE=sandbox`. Missing credentials keep savings disabled. Never use
   a `VITE_` prefix. Production activation requires live provider credentials.
4. Start the backend normally. APScheduler reconciles persisted savings operations
   and synchronizes balances every minute. Database leases and stable provider
   keys coordinate retries across workers. Each lease lasts two minutes.

The application does not apply migrations automatically. Switching modes uses
separate provider IDs, goals and operations. Do not delete pending operations or
rotate their provider keys. Unknown operations retry the exact stored provider
payload with the original `Embed-Idempotency-Key`.

## Buyer and publication behavior

`POST /auth/register` accepts `account_type` (individual by default), plus required
`business_name` and `business_phone` for business customers. `/auth/me` returns
these fields. Business metadata does not change the customer's role. Public admin
registration remains forbidden.

Public catalog, product detail, `POST /products/batch` (`{ids: [...]}`),
`POST /products/validate-cart` (`{items: [{product_id, quantity}]}`), and checkout
exclude unpublished products. Cart validation aggregates repeated product IDs.
`GET /admin/products` is admin-only; `GET /vendors/me/products` is vendor-only and
scoped to that vendor. `GET /products/me` remains a vendor alias.

Only vendors create submissions, always as Pending Approval with their own ID.
Vendor edits, including image edits, reset publication status. Admin edits retain
status. Only admins publish, and publication requires positive stock. Failed
mutations do not retry with a reduced set of fields. Checkout uses the
`marketplace_checkout` RPC to lock rows, revalidate publication, calculate prices,
and commit order, items, stock, wallet debit and payment ledger together.

## Savings onboarding

Every savings endpoint requires a validated customer session. Writes require an
`Idempotency-Key` header (1-200 printable ASCII characters, excluding spaces).

1. `GET /savings/terms` returns the provider terms link for the frontend to display.
2. `POST /savings/account` accepts `{first_name, last_name, phone_number,
   terms_of_use_accepted: true}`. The buyer must explicitly accept the displayed
   terms. Email is read from the trusted profile. Account creation is a singleton
   per buyer/environment, so retries from different tabs cannot create duplicates.
3. `POST /savings/account/identity` accepts `{bvn}`. Send provider test identities
   in sandbox. Raw BVNs and identity responses are never persisted. Verification
   is asynchronous; account readiness is polled from the provider.
4. `POST /savings/account/wallet` creates the buyer's NGN investment wallet after
   verification and terms acceptance. This is also a singleton operation.
5. `GET /savings/status` reports enabled, account_ready, mode and actionable setup
   messages. It checks provider verification, terms, wallet ownership and currency.

Self-service onboarding currently supports individual investment accounts.
Business investment verification is not implemented: business buyers receive a
409 and must contact support. Their marketplace registration alone never marks
an investment account verified. Implement and sandbox-test the provider's business
verification and due-diligence flow before offering business investments.

## Savings goals and retries

- `GET /savings/` reconciles writes, fetches all provider pages and returns only
  goals linked to the authenticated buyer's server-owned savings IDs.
- `POST /savings/` accepts `{name, target_amount_kobo, target_date,
  interest_enabled}`. Amounts must be positive JavaScript-safe integers. Dates
  must use YYYY-MM-DD and be at least 90 days away on initial submission.
- `POST /savings/{id}/fund` accepts `{amount_kobo}`. It derives the wallet and
  product code from server records and rejects another buyer's goal.
- `GET /savings/operations` reconciles pending writes and returns this buyer's
  recent operation IDs, names, request keys, states, responses and timestamps.
  After a reload, consult this endpoint before starting another write. Resume
  pending requests with their existing key; a new key represents a new operation.

The unique operation reservation is scoped by buyer, mode, operation and key.
Different payloads return 409. Provider receipts are stored before local
finalization, so database failures can recover without duplicating provider work.
Processing requests return 409; unknown outcomes return 503 and remain recoverable.
Pending transfers remain pending until provider confirmation. No savings write
deducts the marketplace wallet. Funding uses the verified Cowrywise NGN wallet;
funding that investment wallet from marketplace balances is outside this API.

Goal responses contain only application ID, name, target_amount_kobo,
balance_kobo, maturity_date and status. Confirmed provider balances are synchronized;
fractional-kobo interest is truncated to whole kobo, never rounded up. Provider
outages return an error instead of returning fabricated balances or success.

## Verification and activation limits

Offline regression tests cover authorization, metadata persistence, publication,
cart validation, safe integer amounts, exact funding decimals, provider pagination,
OAuth caching, cross-user access, pending transfers, retry conflicts, timeout
recovery, readiness and maturity. Existing notification, OTP and backend tests are
also run. The SQL and PL/pgSQL are parser-checked. No live database migration,
provider sandbox call or real transfer has been performed by this change.

Before activation, test the migration and checkout concurrency/rollback on staging
PostgreSQL, then verify actual Cowrywise account creation, BVN verification, wallet
creation, goal creation, insufficient funds, provider errors, ambiguous timeouts,
retries across processes, reconciliation, and maturity with sandbox credentials.

Provider documentation used: [authentication](https://developers.cowrywise.com/reference/authentication),
[account onboarding](https://developers.cowrywise.com/guides/investment-accounts),
[identity verification](https://developers.cowrywise.com/reference/update-identity),
[savings](https://developers.cowrywise.com/reference/create-savings),
[funding](https://developers.cowrywise.com/reference/fund-savings),
[transfers](https://developers.cowrywise.com/reference/get-all-transfers), and
[idempotency](https://developers.cowrywise.com/reference/idempotent-request).

To reproduce the offline checks from the repository root, install
`requirements-dev.txt` in a Python 3.12 virtual environment, then run:

```powershell
python test/run_offline.py
```

The runner substitutes dummy Supabase credentials and blocks real HTTP transports;
mock provider transports and in-process FastAPI requests remain available.
