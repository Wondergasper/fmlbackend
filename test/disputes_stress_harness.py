"""
disputes_stress_harness.py — Empirical Stress Harness for Dispute Endpoints
Tests concurrent dispute creation, double refund vulnerabilities, rapid note additions,
audit locking on resolved disputes, and wallet balance precision.
"""

import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from main import app
from dependencies import get_current_user, require_role

client = TestClient(app)

class MockUser:
    def __init__(self, user_id="user_customer_123", email="customer@example.com", role="customer"):
        self.id = user_id
        self.email = email
        self.role = role

mock_customer = MockUser("user_customer_123", "customer@example.com", "customer")
mock_vendor = MockUser("user_vendor_456", "vendor@example.com", "vendor")
mock_admin = MockUser("user_admin_789", "admin@example.com", "admin")

def create_mock_db_chain(return_data=None):
    mock_chain = MagicMock()
    mock_chain.select.return_value = mock_chain
    mock_chain.insert.return_value = mock_chain
    mock_chain.update.return_value = mock_chain
    mock_chain.delete.return_value = mock_chain
    mock_chain.eq.return_value = mock_chain
    mock_chain.or_.return_value = mock_chain
    mock_chain.order.return_value = mock_chain
    mock_chain.single.return_value = mock_chain
    
    mock_result = MagicMock()
    mock_result.data = return_data if return_data is not None else []
    mock_chain.execute.return_value = mock_result
    return mock_chain


# ============================================================================
# Test 1: Concurrent Dispute Creation Vulnerability
# ============================================================================
def test_concurrent_dispute_creation(num_concurrent=10):
    print(f"\n--- [Test 1] Testing Concurrent Dispute Creation ({num_concurrent} requests) ---")
    app.dependency_overrides[get_current_user] = lambda: mock_customer

    disputes_created = []
    lock = threading.Lock()

    order_mock_data = {"id": "ord_concurrent_1", "customer_id": mock_customer.id}
    profile_mock_data = {"role": "customer"}

    def table_side_effect(table_name):
        if table_name == "orders":
            return create_mock_db_chain(order_mock_data)
        elif table_name == "profiles":
            return create_mock_db_chain(profile_mock_data)
        return create_mock_db_chain([])

    def admin_table_side_effect(table_name):
        if table_name == "disputes":
            # Mock generating unique dispute ID for each insert
            new_id = f"disp_{len(disputes_created) + 1:03d}"
            mock_disp = [{
                "id": new_id,
                "order_id": "ord_concurrent_1",
                "created_by": mock_customer.id,
                "reason": "Defective product",
                "status": "Open"
            }]
            with lock:
                disputes_created.append(mock_disp[0])
            return create_mock_db_chain(mock_disp)
        elif table_name == "dispute_notes":
            return create_mock_db_chain([{"id": "note_init"}])
        return create_mock_db_chain([])

    with patch("routers.disputes.supabase") as mock_sp, patch("routers.disputes.supabase_admin") as mock_sp_admin:
        mock_sp.table.side_effect = table_side_effect
        mock_sp_admin.table.side_effect = admin_table_side_effect

        def send_open_dispute(idx):
            tc = TestClient(app)
            return tc.post("/disputes/", json={"order_id": "ord_concurrent_1", "reason": f"Defective product {idx}"})

        start_time = time.time()
        with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
            futures = [executor.submit(send_open_dispute, i) for i in range(num_concurrent)]
            responses = [f.result() for f in as_completed(futures)]
        duration = time.time() - start_time

    success_responses = [r for r in responses if r.status_code == 201]
    print(f"Executed {num_concurrent} concurrent creation requests in {duration:.3f}s")
    print(f"Successful creation responses (201 Created): {len(success_responses)}/{num_concurrent}")
    print(f"Total dispute records created in DB for same order: {len(disputes_created)}")

    is_vulnerable = len(disputes_created) > 1
    print(f"Vulnerability Status: {'VULNERABLE (Duplicate disputes created for single order)' if is_vulnerable else 'SECURE'}")

    app.dependency_overrides.clear()
    return {
        "num_requested": num_concurrent,
        "successful_creations": len(success_responses),
        "total_disputes_in_db": len(disputes_created),
        "vulnerable": is_vulnerable
    }


# ============================================================================
# Test 2: Double Refund / Re-resolution Vulnerability
# ============================================================================
def test_double_refund_on_resolution():
    print(f"\n--- [Test 2] Testing Double Refund / Re-resolution Vulnerability ---")
    app.dependency_overrides[get_current_user] = lambda: mock_admin

    refund_events = []
    lock = threading.Lock()

    dispute_id = "disp_double_ref_1"
    order_id = "ord_ref_100"
    order_amount_kobo = 500_000 # ₦5,000
    
    # Track simulated wallet balance
    customer_wallet = {"balance": 100_000} # ₦1,000 initial

    def table_side_effect(table_name):
        if table_name == "orders":
            return create_mock_db_chain({
                "id": order_id,
                "customer_id": mock_customer.id,
                "total_kobo": order_amount_kobo,
                "order_items": [{"products": {"vendor_id": mock_vendor.id}}]
            })
        elif table_name == "profiles":
            return create_mock_db_chain({"wallet_balance": customer_wallet["balance"]})
        return create_mock_db_chain([])

    def admin_table_side_effect(table_name):
        if table_name == "disputes":
            return create_mock_db_chain([{
                "id": dispute_id,
                "order_id": order_id,
                "status": "Resolved",
                "resolution_outcome": "Refund Buyer",
                "resolution_notes": "Refund approved"
            }])
        elif table_name == "profiles":
            # Intercept update call to observe wallet credits
            class WalletUpdateChain:
                def update(self, data):
                    with lock:
                        customer_wallet["balance"] = data["wallet_balance"]
                        refund_events.append(data["wallet_balance"])
                    m = MagicMock()
                    m.eq.return_value.execute.return_value = MagicMock(data=[data])
                    return m
            return WalletUpdateChain()
        elif table_name in ["dispute_notes", "wallet_transactions"]:
            return create_mock_db_chain([{"id": "tx_mock"}])
        return create_mock_db_chain([])

    with patch("routers.disputes.supabase") as mock_sp, patch("routers.disputes.supabase_admin") as mock_sp_admin:
        mock_sp.table.side_effect = table_side_effect
        mock_sp_admin.table.side_effect = admin_table_side_effect

        tc = TestClient(app)
        payload = {"resolution_outcome": "Refund Buyer", "resolution_notes": "First refund resolution"}
        
        # Call 1
        resp1 = tc.patch(f"/disputes/{dispute_id}/resolve", json=payload)
        bal_after_first = customer_wallet["balance"]

        # Call 2 (re-resolving already resolved dispute)
        payload2 = {"resolution_outcome": "Refund Buyer", "resolution_notes": "Second refund resolution call"}
        resp2 = tc.patch(f"/disputes/{dispute_id}/resolve", json=payload2)
        bal_after_second = customer_wallet["balance"]

    print(f"Initial Balance: ₦{100000/100:,.2f}")
    print(f"Balance after 1st Resolution: ₦{bal_after_first/100:,.2f}")
    print(f"Balance after 2nd Resolution: ₦{bal_after_second/100:,.2f}")
    print(f"Total Wallet Updates Executed: {len(refund_events)}")

    is_vulnerable = bal_after_second > bal_after_first
    print(f"Vulnerability Status: {'VULNERABLE (Double refund allowed on resolved dispute)' if is_vulnerable else 'SECURE'}")

    app.dependency_overrides.clear()
    return {
        "initial_balance_kobo": 100_000,
        "balance_after_first_kobo": bal_after_first,
        "balance_after_second_kobo": bal_after_second,
        "vulnerable": is_vulnerable
    }


# ============================================================================
# Test 3: Rapid Note Additions & Resolved Dispute Audit Lock
# ============================================================================
def test_rapid_note_additions_and_audit_lock(num_notes=30):
    print(f"\n--- [Test 3] Testing Rapid Note Additions & Resolved Dispute Audit Lock ({num_notes} notes) ---")
    app.dependency_overrides[get_current_user] = lambda: mock_customer

    notes_inserted = []
    lock = threading.Lock()

    dispute_mock = {"id": "disp_notes_1", "status": "Resolved"} # Dispute is already RESOLVED!
    profile_mock = {"role": "customer"}

    def table_side_effect(table_name):
        if table_name == "disputes":
            return create_mock_db_chain(dispute_mock)
        elif table_name == "profiles":
            return create_mock_db_chain(profile_mock)
        return create_mock_db_chain([])

    def admin_table_side_effect(table_name):
        if table_name == "dispute_notes":
            class NoteInsertChain:
                def insert(self, data):
                    with lock:
                        notes_inserted.append(data)
                    m = MagicMock()
                    m.execute.return_value = MagicMock(data=[data])
                    return m
            return NoteInsertChain()
        return create_mock_db_chain([])

    with patch("routers.disputes.supabase") as mock_sp, patch("routers.disputes.supabase_admin") as mock_sp_admin:
        mock_sp.table.side_effect = table_side_effect
        mock_sp_admin.table.side_effect = admin_table_side_effect

        def send_note(idx):
            tc = TestClient(app)
            return tc.post("/disputes/disp_notes_1/notes", json={"note_text": f"Rapid note #{idx}"})

        start_time = time.time()
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(send_note, i) for i in range(num_notes)]
            responses = [f.result() for f in as_completed(futures)]
        duration = time.time() - start_time

        # Test whitespace-only note
        tc = TestClient(app)
        ws_response = tc.post("/disputes/disp_notes_1/notes", json={"note_text": "   "})

    success_count = len([r for r in responses if r.status_code == 201])
    print(f"Sent {num_notes} rapid notes in {duration:.3f}s")
    print(f"Notes successfully accepted on RESOLVED dispute: {success_count}/{num_notes}")
    print(f"Whitespace-only note status code: {ws_response.status_code} (Accepted: {ws_response.status_code == 201})")

    audit_lock_vulnerable = success_count > 0
    whitespace_vulnerable = ws_response.status_code == 201

    print(f"Audit Lock Status: {'VULNERABLE (Notes can be posted to resolved disputes)' if audit_lock_vulnerable else 'SECURE'}")
    print(f"Whitespace Validation: {'VULNERABLE (Blank notes accepted)' if whitespace_vulnerable else 'SECURE'}")

    app.dependency_overrides.clear()
    return {
        "rapid_notes_sent": num_notes,
        "rapid_notes_accepted": success_count,
        "whitespace_note_accepted": whitespace_vulnerable,
        "audit_lock_vulnerable": audit_lock_vulnerable
    }


# ============================================================================
# Test 4: Wallet Balance Precision & Split Rounding Discrepancy
# ============================================================================
def test_wallet_split_precision():
    print(f"\n--- [Test 4] Testing Wallet Split Precision & Rounding Discrepancy ---")
    app.dependency_overrides[get_current_user] = lambda: mock_admin

    dispute_id = "disp_split_1"
    order_id = "ord_split_odd"
    total_kobo = 100_005 # ₦1,000.05 (Odd number of kobo)

    customer_credited = 0
    vendor_credited = 0

    def table_side_effect(table_name):
        if table_name == "orders":
            return create_mock_db_chain({
                "id": order_id,
                "customer_id": mock_customer.id,
                "total_kobo": total_kobo,
                "order_items": [{"products": {"vendor_id": mock_vendor.id}}]
            })
        elif table_name == "profiles":
            return create_mock_db_chain({"wallet_balance": 0})
        return create_mock_db_chain([])

    def admin_table_side_effect(table_name):
        nonlocal customer_credited, vendor_credited
        if table_name == "disputes":
            return create_mock_db_chain([{
                "id": dispute_id,
                "order_id": order_id,
                "status": "Resolved",
                "resolution_outcome": "Split",
                "resolution_notes": "50/50 split"
            }])
        elif table_name == "profiles":
            class WalletUpdateChain:
                def update(self, data):
                    m = MagicMock()
                    class EqChain:
                        def eq(self, field, val):
                            nonlocal customer_credited, vendor_credited
                            if val == mock_customer.id:
                                customer_credited = data["wallet_balance"]
                            elif val == mock_vendor.id:
                                vendor_credited = data["wallet_balance"]
                            return MagicMock(execute=lambda: MagicMock(data=[data]))
                    return EqChain()
            return WalletUpdateChain()
        elif table_name in ["dispute_notes", "wallet_transactions"]:
            return create_mock_db_chain([{"id": "tx_mock"}])
        return create_mock_db_chain([])

    with patch("routers.disputes.supabase") as mock_sp, patch("routers.disputes.supabase_admin") as mock_sp_admin:
        mock_sp.table.side_effect = table_side_effect
        mock_sp_admin.table.side_effect = admin_table_side_effect

        tc = TestClient(app)
        payload = {"resolution_outcome": "Split", "resolution_notes": "50/50 split"}
        resp = tc.patch(f"/disputes/{dispute_id}/resolve", json=payload)

    total_refunded = customer_credited + vendor_credited
    discrepancy = total_kobo - total_refunded

    print(f"Original Order Total: {total_kobo} kobo (₦{total_kobo/100:.2f})")
    print(f"Customer Credited: {customer_credited} kobo")
    print(f"Vendor Credited:   {vendor_credited} kobo")
    print(f"Total Distributed: {total_refunded} kobo")
    print(f"Precision Loss (Discrepancy): {discrepancy} kobo")

    is_vulnerable = discrepancy != 0
    print(f"Precision Status: {'VULNERABLE (1 kobo lost due to integer division //)' if is_vulnerable else 'EXACT'}")

    app.dependency_overrides.clear()
    return {
        "order_total_kobo": total_kobo,
        "customer_credited": customer_credited,
        "vendor_credited": vendor_credited,
        "discrepancy_kobo": discrepancy,
        "vulnerable": is_vulnerable
    }


if __name__ == "__main__":
    print("==================================================")
    print("  EMPIRICAL DISPUTE ENDPOINTS STRESS HARNESS    ")
    print("==================================================")
    r1 = test_concurrent_dispute_creation(10)
    r2 = test_double_refund_on_resolution()
    r3 = test_rapid_note_additions_and_audit_lock(30)
    r4 = test_wallet_split_precision()
    print("\n==================================================")
    print("               STRESS TEST SUMMARY                ")
    print("==================================================")
    print(json.dumps({
        "concurrent_creation": r1,
        "double_refund": r2,
        "rapid_notes": r3,
        "wallet_precision": r4
    }, indent=2))
