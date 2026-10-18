"""Real-life API test for Farm-Connect marketplace."""
import requests, sys, os
from dotenv import load_dotenv
load_dotenv()

BASE = "http://localhost:8000"
PASS = 0
FAIL = 0

def test(name, method, path, expect, **kwargs):
    global PASS, FAIL
    url = f"{BASE}{path}"
    try:
        r = requests.request(method, url, timeout=10, **kwargs)
        ok = r.status_code == expect
        if ok:
            PASS += 1
        else:
            FAIL += 1
            print(f"  FAIL [{r.status_code}] expected {expect}")
            if r.headers.get("content-type","").startswith("application/json"):
                print(f"       {r.text[:200]}")
        return r
    except Exception as e:
        FAIL += 1
        print(f"  FAIL [error] {e}")
        return None

# ── Register ──
print("=== 1. REGISTER CUSTOMER ===")
r = test("Register", "POST", "/auth/register", 201, json={
    "email": "chioma.customer@gmail.com", "password": "Test1234!",
    "full_name": "Chioma Okafor", "role": "customer", "phone": "08031234567"
})
if r: print(f"  User: {r.json().get('user_id','?')[:12]}...")

print("\n=== 2. LOGIN ===")
r = test("Login", "POST", "/auth/login", 200, json={
    "email": "gasperwonder@gmail.com", "password": "Test1234!"
})
token = r.json().get("access_token","") if r else ""
if r:
    user_data = r.json().get("user", {})
    print(f"  Token: {token[:30]}... | Role: {user_data.get('role','')}")

# Obtain Admin token for admin endpoints
r_admin = requests.post(f"{BASE}/auth/login", json={
    "email": "admin@farmconnect.ng", "password": "Test1234!"
})
admin_token = r_admin.json().get("access_token", "") if r_admin.status_code == 200 else ""

print("\n=== 3. LIST PRODUCTS (public) ===")
r = test("Products", "GET", "/products", 200)
if r:
    prods = r.json()
    print(f"  Count: {len(prods)}")
    for p in prods[:4]:
        print(f"  - {p['name']}  N{p['price']/100:,.2f}  ({p['stock']} left)")

print("\n=== 4. PRODUCT DETAIL ===")
r = test("Products", "GET", "/products", 200)
if r and len(r.json()):
    pid = r.json()[0]["id"]
    r2 = test("Detail", "GET", f"/products/{pid}", 200)
    if r2:
        p2 = r2.json()
        print(f"  {p2['name']} | N{p2['price']/100:,.2f} | Stock: {p2['stock']} | {p2['description'][:60]}")

print("\n=== 5. WALLET BALANCE ===")
headers = {"Authorization": f"Bearer {token}"}
r = test("Balance", "GET", "/wallet/balance", 200, headers=headers)
if r: print(f"  Balance: N{r.json()['balance_kobo']/100:,.2f}")

print("\n=== 6. VENDOR LIST ===")
r = test("Vendors", "GET", "/vendors", 200, headers={"Authorization": f"Bearer {admin_token}"})
if r:
    for v in r.json():
        print(f"  - {v.get('farm_name',v.get('display_name','?'))} | N{v.get('wallet_balance',0)/100:,.2f}")

print("\n=== 7. PLACE ORDER ===")
r = test("Products", "GET", "/products", 200)
if r and len(r.json()):
    prods = r.json()
    items = [
        {"product_id": prods[0]["id"], "quantity": 2, "unit_price": prods[0]["price"]},
        {"product_id": prods[1]["id"] if len(prods) > 1 else prods[0]["id"], "quantity": 1, "unit_price": prods[1]["price"] if len(prods) > 1 else prods[0]["price"]}
    ]
    order = {
        "items": items,
        "delivery_type": "standard",
        "delivery_address": "42 Awolowo Road, Ikoyi, Lagos",
        "notes": "Leave with security at gate"
    }
    r2 = test("Place Order", "POST", "/orders", 201, headers=headers, json=order)
    if r2:
        print(f"  Order ID: {r2.json().get('order_id','?')[:12]}...")
        print(f"  Total: N{r2.json().get('total_kobo',0)/100:,.2f}")

print("\n=== 8. MY ORDERS ===")
r = test("My Orders", "GET", "/orders", 200, headers=headers)
if r:
    orders = r.json()
    print(f"  Orders: {len(orders)}")
    for o in orders[:3]:
        print(f"  - {o['id'][:12]}... | N{o['total_kobo']/100:,.2f} | {o['status']}")

print("\n=== 9. CATEGORIES (public) ===")
r = test("Categories", "GET", "/admin/categories", 200, headers={"Authorization": f"Bearer {admin_token}"})
if r and r.status_code == 200:
    cats = r.json()
    print(f"  Categories: {len(cats)}")
    for c in cats:
        print(f"  - {c}")

print("\n=== 10. HEALTH CHECK (root) ===")
r = test("Root", "GET", "/", 200)

print(f"\n{'='*40}")
print(f"  PASSED: {PASS}  FAILED: {FAIL}  TOTAL: {PASS+FAIL}")
print(f"{'='*40}")
