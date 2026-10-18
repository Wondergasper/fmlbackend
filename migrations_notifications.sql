-- Notifications Table Migration

-- 1. Create Notifications Table
CREATE TABLE IF NOT EXISTS notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    type VARCHAR(50) NOT NULL DEFAULT 'system', -- 'order', 'wallet', 'product', 'dispute', 'system'
    link VARCHAR(255) NULL,                     -- e.g. '/dashboard/customer/orders'
    is_read BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB NULL,                        -- { "order_id": "...", "amount_kobo": 10000 }
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 2. Indexes for fast unread queries and sorted retrieval
CREATE INDEX IF NOT EXISTS idx_notifications_user_read ON notifications(user_id, is_read, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at DESC);

-- 3. Row Level Security (RLS)
ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;

-- Allow users to view only their own notifications
CREATE POLICY "Users can view own notifications"
ON notifications FOR SELECT
TO authenticated
USING (auth.uid() = user_id);

-- Allow users to update their own notifications (e.g. mark as read)
CREATE POLICY "Users can update own notification read status"
ON notifications FOR UPDATE
TO authenticated
USING (auth.uid() = user_id);

-- Allow system/service role to insert notifications
CREATE POLICY "Service role can insert notifications"
ON notifications FOR INSERT
TO authenticated
WITH CHECK (true);
