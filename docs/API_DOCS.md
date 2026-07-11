# Farmers Market API — Reference

Base URL: `/` (FastAPI app)

Authentication
- POST `/auth/register` — Register a new user (customer or vendor). Body: `email`, `password`, `full_name`, `role` (customer|vendor), optional `phone`. Returns `user_id` and message.
- POST `/auth/login` — Login with `email` and `password`. Returns `access_token` (Bearer) and `user` info.
- GET `/auth/me` — Get current user profile. Requires `Authorization: Bearer <token>`.
- POST `/auth/logout` — Invalidate session (requires auth).
- POST `/auth/send-otp` — Send a 4-digit OTP to email. Body: `email`.
- POST `/auth/verify-otp` — Verify OTP. Body: `email`, `otp_code`.

Products
- GET `/products/` — List products (filters may be applied by query).
- POST `/products/` — Create a product (vendor-only). Body: `name`, `description`, `price`, `stock`, `category`, `origin`, etc.
- GET `/products/{product_id}` — Get product details.
- PATCH `/products/{product_id}/status` — Update product status (admin). Body: `status` (Approved|Rejected) and optional `reason`.

Orders
- GET `/orders/` — List orders for caller (customer sees own, vendor sees vendor orders, admin sees all).
- POST `/orders/` — Place a new order (customer-only). Body: `items` (product_id, quantity, unit_price), `delivery_address`, `delivery_type`.
- GET `/orders/{order_id}` — Get order details (permissioned).
- PATCH `/orders/{order_id}/status` — Update order status (vendor/admin). Body: `status` (Processing|In Transit|Delivered|Cancelled), optional `note`.

Vendors
- GET `/vendors/` — List vendors / vendor profiles.
- PATCH `/vendors/{vendor_id}/status` — Update vendor status (admin). Body: `status` (Active|Suspended), optional `reason`.

Wallet
- POST `/wallet/topup` — Top up wallet (customer). Body: `amount_kobo`, `reference`.
- GET `/wallet/` — Wallet balance and history endpoints (see implementation).

Uploads
- POST `/uploads/` — Upload product images to Supabase Storage (multipart/form-data).

Analytics
- GET `/analytics/` — KPI and revenue endpoints (admin/vendor views depend on role).

Disputes
- POST/GET `/disputes/` — Create and manage dispute records and resolution flow.

WebSockets
- WS `/ws/orders/{order_id}` — Real-time order tracking and vendor alerts (see `services.websocket_manager`).

Notes
- All endpoints that require authentication use `dependencies.get_current_user` (HTTP Bearer). Role enforcement uses `require_role([...])`.
- Background jobs use Celery tasks in `services/email.py` and others (methods call `.delay(...)`).
- For full request/response examples, consult the router source files in `routers/`.
