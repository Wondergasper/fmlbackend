-- Logistics & Dispatch Tables Migration

-- 1. Create Hubs Table
CREATE TABLE IF NOT EXISTS hubs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) UNIQUE NOT NULL,
    capacity INTEGER DEFAULT 0,
    eta VARCHAR(50),
    zone VARCHAR(100),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 2. Create Riders Table
CREATE TABLE IF NOT EXISTS riders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    phone VARCHAR(50) NOT NULL,
    hub_name VARCHAR(255) REFERENCES hubs(name) ON DELETE SET NULL,
    status VARCHAR(50) DEFAULT 'Active',
    assigned_order_id UUID REFERENCES orders(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 3. Create Indexes for performance
CREATE INDEX IF NOT EXISTS idx_riders_status ON riders(status);
CREATE INDEX IF NOT EXISTS idx_riders_hub ON riders(hub_name);

-- 4. Enable Row Level Security (RLS) if required by the main app structure
-- For now, the FastAPI backend will bypass RLS by using the service role key,
-- but if direct client access is needed, policies should be added here.
ALTER TABLE hubs ENABLE ROW LEVEL SECURITY;
ALTER TABLE riders ENABLE ROW LEVEL SECURITY;

-- Allow read access to authenticated users if needed, 
-- but modifications should go through FastAPI admin endpoints.
CREATE POLICY "Allow read access to authenticated users" 
ON hubs FOR SELECT 
TO authenticated 
USING (true);

CREATE POLICY "Allow read access to authenticated users" 
ON riders FOR SELECT 
TO authenticated 
USING (true);
