-- ============================================================================
-- Migration: Update profiles role check constraint for unified RBAC
-- Run this in your Supabase / PostgreSQL SQL Editor
-- ============================================================================

-- 1. Drop existing role check constraint if it exists
ALTER TABLE profiles 
DROP CONSTRAINT IF EXISTS profiles_role_check;

-- 2. Add updated check constraint supporting all platform roles and aliases
ALTER TABLE profiles 
ADD CONSTRAINT profiles_role_check 
CHECK (role IN ('customer', 'vendor', 'admin', 'rider', 'driver', 'courier', 'logist'));

-- 3. Normalize existing records where applicable
-- (e.g. Map legacy 'driver' / 'courier' to canonical 'rider')
UPDATE profiles 
SET role = 'rider' 
WHERE role IN ('driver', 'courier');

-- 4. Create index on role if not exists
CREATE INDEX IF NOT EXISTS idx_profiles_role ON profiles(role);
