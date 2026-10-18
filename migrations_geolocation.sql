-- Geolocation Telemetry & Breadcrumbs Migration

-- 1. Create Order Tracking Breadcrumbs Table
CREATE TABLE IF NOT EXISTS order_tracking_breadcrumbs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id VARCHAR(64) NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    driver_id VARCHAR(64) NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    heading DOUBLE PRECISION,
    speed DOUBLE PRECISION,
    accuracy DOUBLE PRECISION,
    provider VARCHAR(50) DEFAULT 'gps',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 2. Indexes for performance on real-time queries and route replay
CREATE INDEX IF NOT EXISTS idx_breadcrumbs_order_created ON order_tracking_breadcrumbs(order_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_breadcrumbs_driver_created ON order_tracking_breadcrumbs(driver_id, created_at DESC);

-- 3. Row Level Security (RLS)
ALTER TABLE order_tracking_breadcrumbs ENABLE ROW LEVEL SECURITY;

-- Allow read access to authenticated users
CREATE POLICY "Allow read access to authenticated users for breadcrumbs"
ON order_tracking_breadcrumbs FOR SELECT
TO authenticated
USING (true);

-- Allow insert by service role / authenticated drivers
CREATE POLICY "Allow insert access for breadcrumbs"
ON order_tracking_breadcrumbs FOR INSERT
TO authenticated
WITH CHECK (true);
