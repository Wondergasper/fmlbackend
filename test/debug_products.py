"""Test products query with different clients."""
from database import supabase, supabase_admin

# Anon without join
r1 = supabase.table("products").select("id, name, price, status").eq("status", "Approved").execute()
print(f"Anon (no join): {len(r1.data)} products")
for p in r1.data[:3]:
    print(f"  {p['name']}")

# Anon with join
r2 = supabase.table("products").select("*, vendor:profiles!vendor_id(status)").eq("status", "Approved").execute()
print(f"Anon (with join): {len(r2.data)} products")

# Admin
r3 = supabase_admin.table("products").select("id, name, price, status").eq("status", "Approved").execute()
print(f"Admin (no join): {len(r3.data)} products")
print(f"Admin total: {supabase_admin.table('products').select('count', count='exact').execute().count}")
