-- OTP & email verification columns for profiles table
-- Run this in your Supabase SQL Editor

ALTER TABLE profiles
  ADD COLUMN IF NOT EXISTS otp_code TEXT,
  ADD COLUMN IF NOT EXISTS otp_expires_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS otp_purpose TEXT,
  ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE;

-- Index for fast OTP lookup by email
CREATE INDEX IF NOT EXISTS idx_profiles_email ON profiles (email);

-- Update existing users as verified (they were created before OTP was introduced)
UPDATE profiles SET email_verified = TRUE WHERE email_verified IS NULL;
