-- ============================================================================
-- Farm-Connect Dispute Resolution Audit Trail Schema
-- ============================================================================

-- 1. Disputes table
CREATE TABLE IF NOT EXISTS disputes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    created_by UUID REFERENCES profiles(id),
    opened_by UUID REFERENCES profiles(id),
    reason TEXT NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'Open', -- 'Open', 'Under Review', 'Resolved', 'Closed'
    resolution_outcome VARCHAR(100) NULL,      -- 'Refund Buyer', 'Release to Vendor', 'Split'
    resolution_notes TEXT NULL,
    resolved_by UUID NULL REFERENCES profiles(id),
    resolved_at TIMESTAMP WITH TIME ZONE NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_disputes_order ON disputes(order_id);
CREATE INDEX IF NOT EXISTS idx_disputes_created_by ON disputes(created_by);
CREATE INDEX IF NOT EXISTS idx_disputes_opened_by ON disputes(opened_by);
CREATE INDEX IF NOT EXISTS idx_disputes_status ON disputes(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_disputes_active_order ON disputes(order_id) WHERE status IN ('Open', 'Under Review');

-- 2. Dispute Notes table (Audit trail of comments between buyer, vendor, and admin)
CREATE TABLE IF NOT EXISTS dispute_notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispute_id UUID NOT NULL REFERENCES disputes(id) ON DELETE CASCADE,
    user_id UUID REFERENCES profiles(id),
    sender_id UUID REFERENCES profiles(id),
    user_role VARCHAR(50) NOT NULL, -- 'customer', 'vendor', 'admin'
    sender_role VARCHAR(50) NULL,
    note_text TEXT NOT NULL,
    note TEXT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dispute_notes_dispute ON dispute_notes(dispute_id);

-- 3. Dispute Evidence table (Photo/Document uploads linked to dispute)
CREATE TABLE IF NOT EXISTS dispute_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispute_id UUID NOT NULL REFERENCES disputes(id) ON DELETE CASCADE,
    uploaded_by UUID NOT NULL REFERENCES profiles(id),
    file_path TEXT NOT NULL,
    file_url TEXT NOT NULL,
    file_type VARCHAR(50) DEFAULT 'image',
    description TEXT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dispute_evidence_dispute ON dispute_evidence(dispute_id);
