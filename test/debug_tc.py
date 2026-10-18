"""Test whether TestClient vs direct supabase query gives different results."""
from fastapi.testclient import TestClient
from main import app
from database import supabase

# 1. Direct supabase query
r1 = supabase.table("products").select("*").eq("status", "Approved").execute()
print(f"Direct query: {len(r1.data)} products")

# 2. Via TestClient
client = TestClient(app)
r2 = client.get("/products")
print(f"Via TestClient: {r2.status_code}, {len(r2.json())} products")

# 3. Via TestClient again
r3 = client.get("/products")
print(f"Via TestClient (2nd): {r3.status_code}, {len(r3.json())} products")

# 4. Direct query after TestClient
r4 = supabase.table("products").select("*").eq("status", "Approved").execute()
print(f"Direct query again: {len(r4.data)} products")
