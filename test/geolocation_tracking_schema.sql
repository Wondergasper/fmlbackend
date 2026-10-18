-- ============================================================================
-- Geolocation Delivery Tracking — Schema Migration
-- Run this in your Supabase SQL Editor to add tracking columns to orders.
-- ============================================================================

-- Add driver assignment column
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS driver_id UUID REFERENCES profiles(id) ON DELETE SET NULL;

-- Add geocoded destination coordinates (set when order is placed)
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS latitude  NUMERIC(10, 7),
    ADD COLUMN IF NOT EXISTS longitude NUMERIC(10, 7);

-- Add real-time driver location (updated as driver moves)
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS driver_latitude  NUMERIC(10, 7),
    ADD COLUMN IF NOT EXISTS driver_longitude NUMERIC(10, 7);

-- Add cached ETA / distance from Google Distance Matrix API
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS eta_minutes   INTEGER,
    ADD COLUMN IF NOT EXISTS distance_text TEXT;

-- Index for fast driver-to-order lookups
CREATE INDEX IF NOT EXISTS idx_orders_driver ON orders(driver_id);

-- ============================================================================
-- NOTE: No RLS changes needed — supabase_admin (service role) bypasses RLS.
-- The application layer enforces access control for location updates.
-- ============================================================================
