from supabase import create_client, Client

from config import settings

# Public client for general actions
supabase: Client = create_client(settings.supabase_url, settings.supabase_key)

# Admin client with superuser privileges (to bypass RLS and adjust balances)
supabase_admin: Client = create_client(settings.supabase_url, settings.supabase_service_role_key)
