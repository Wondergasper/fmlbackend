# Backend implementation needed for marketplace updates

This is the frontend handoff contract for the marketplace updates. This
checkout contains the FastAPI backend implementation. See
[backend setup and activation limits](docs/MARKETPLACE_SETUP.md) for migrations,
configuration, supported verification flows, and deployment prerequisites.
The frontend-specific paths and verification commands below refer to its
separate React/Vite repository. These changes have not been deployed.

## Farmer portal

Share /farmer/login and /farmer/signup directly with farmers. The dashboard
is /farmer/dashboard, accessible only to the server-validated vendor role.
Public login/signup and the footer have no farmer portal link. Legacy URLs
redirect for existing bookmarks. Portal routes use noindex and robots exclusions.
This is a separate route in the same application, not a separate hostname;
URL secrecy and robots rules do not replace backend authorization.

## Buyer onboarding

POST /auth/register now submits role: customer, account_type: individual | business.
Business registration also submits business_name and business_phone.
Persist and validate these fields in FastAPI and return them from /auth/me.
Both buyer types retain customer permissions. Business metadata must not grant
admin or vendor privileges. Keep farmer registration role: vendor at the
dedicated portal; keep admin creation restricted to an authorized super admin.

## Farmer submissions and super admin publication

The existing admin role represents the publishing super admin in this frontend.
The backend must authorize that permission from its own trusted user records.

| Endpoint | Required behavior |
| --- | --- |
| GET /products/ | Public approved products with positive stock only; always include status. |
| GET /admin/products | Super admin only; return all farmer submissions including Pending Approval and Rejected. |
| GET /vendors/me/products | Vendor only; return only the authenticated farmer's submissions, including unpublished ones. |
| POST /products/ | Vendor creates own submission; backend forces Pending Approval and derives vendor_id. |
| PATCH /products/:id | Authorize owner/admin; farmer edits force Pending Approval, admin edits retain publication status. |
| PATCH /products/:id/status | Super admin only; Approved publishes, Rejected removes availability. Require positive stock to publish. |

The authenticated list endpoints return product arrays in the same shape as the
existing catalog: id, name, category, price (integer kobo), stock, status,
vendor_id, origin, description, image_url; optional vendor_name.
Enforce publication rules on product detail, batch lookup, cart validation,
and checkout too. The frontend filter is defense in depth, not authorization.
Publish/edit failures must leave database state unchanged.

## Target savings API contract

Implement these endpoints in the existing FastAPI backend. Each requires a
validated customer session. No provider IDs are accepted from browser payloads.

| Endpoint | Response or input |
| --- | --- |
| GET /savings/status | { enabled: boolean, account_ready: boolean, mode: sandbox or production, message?: string } |
| GET /savings/ | Array of this buyer's goals; synchronize balances from Cowrywise before returning. |
| POST /savings/ | { name, target_amount_kobo, target_date: YYYY-MM-DD, interest_enabled: boolean }; returns saved goal with id. |
| POST /savings/:id/fund | { amount_kobo }; returns { status: pending or success, reference }. |

A goal response is { id, name, target_amount_kobo, balance_kobo,
maturity_date: YYYY-MM-DD, status: active or matured or completed }.
Validate positive safe integer kobo amounts and a maturity at least 90 days away.
Target amount and goal name are application metadata stored in your database.
Never fabricate a success when the provider is unavailable.

Each write sends Idempotency-Key. Persist an operation row keyed by
authenticated user + operation + key, and compare its payload hash.
Different payloads using the same key must return 409. Reserve operations
atomically before provider calls, persist the result, and reconcile requests
whose outcome is unknown. Derive the same provider key on every retry;
Cowrywise expects the Embed-Idempotency-Key header. Do not count a Pending
transfer as settled or deduct the marketplace wallet optimistically.

Store the user's verified Cowrywise account_id and NGN wallet_id, and each
goal's savings_id/product_code in the server database. Resolve all of these
through the authenticated user and check ownership before funding.
Complete Cowrywise account onboarding/verification and terms acceptance
through the backend. Marketplace buyer signup is separate from investment
account verification. When not ready return account_ready: false and a useful
message. Add a supported verification flow when the backend is available.

server/cowrywise.mjs provides a server-only JavaScript Embed adapter for
OAuth token caching, creating savings, paginated listing, and wallet transfers,
with exact kobo conversion and provider idempotency headers. It has no public
HTTP routes, storage, or authentication middleware and is not imported into
the Vite bundle. Port/wrap it in the FastAPI backend, or call it from a trusted
Node backend service. Never expose it directly as an unauthenticated endpoint.
Paginated provider listing is integration-wide: filter by server-owned IDs
before returning data. Reconcile transfers server-side and expose confirmed
balances. A retry after reloading the frontend must also consult the backend's
pending operations, since the current tab retains keys only during its lifetime.

Server-only environment variables:
- COWRYWISE_CLIENT_ID
- COWRYWISE_CLIENT_SECRET
- COWRYWISE_MODE=sandbox (switch to production only after provider activation)

Do not prefix these secrets with VITE_ or add them to frontend .env files.
Production requires a completed Cowrywise company profile and live credentials.

## Verification

Run node node_modules/vitest/vitest.mjs run --no-file-parallelism and
node node_modules/vite/bin/vite.js build.
Demo mode (VITE_USE_MOCK=true) supports submitting, publishing, buyer types,
and creating/funding goals in memory. Savings explicitly displays sandbox mode;
no real funds move and demo data resets with a page reload.

Before live activation test with Cowrywise sandbox credentials: account
verification, goal creation, funding with exact decimals, insufficient funds,
provider errors, ambiguous timeouts, idempotent retries, duplicate keys,
cross-user goal access, reconciliation, and maturity. Live integration has not
been verified because credentials and the backend are absent from this checkout.

Provider references checked:
- https://developers.cowrywise.com/reference/authentication
- https://developers.cowrywise.com/reference/create-savings
- https://developers.cowrywise.com/guides/savings
- https://developers.cowrywise.com/reference/get-savings
- https://developers.cowrywise.com/reference/fund-savings
- https://developers.cowrywise.com/reference/idempotent-request
