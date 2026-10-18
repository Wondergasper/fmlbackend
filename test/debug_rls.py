"""Debug anon vs admin product visibility."""
from database import supabase, supabase_admin

# Check what admin sees
r = supabase_admin.table("products").select("*").execute()
print(f"Admin sees {len(r.data)} products:")
for p in r.data[:3]:
    print(f"  {p['id'][:8]}... {p['name']} status={p['status']} vendor_id={p['vendor_id'][:8]}...")

# Check what anon sees (no filters)
r2 = supabase.table("products").select("*").execute()
print(f"\nAnon sees {len(r2.data)} products (unfiltered):")
for p in r2.data[:3]:
    print(f"  {p['id'][:8]}... {p['name']} status={p['status']}")

# Check if RLS is blocking
r3 = supabase.table("products").select("count", count="exact").execute()
print(f"\nAnon count: {r3.count}")

# Check what statuses the admin sees
r4 = supabase_admin.table("products").select("status").execute()
statuses = [p["status"] for p in r4.data]
print(f"\nStatuses (admin): {set(statuses)}")

# Check profiles
r5 = supabase_admin.table("profiles").select("id, role, status").execute()
print(f"\nProfiles (admin):")
for p in r5.data:
    print(f"  {p['id'][:8]}... role={p['role']} status={p.get('status','?')}")
