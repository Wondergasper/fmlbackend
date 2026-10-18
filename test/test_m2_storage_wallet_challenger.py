"""
test_m2_storage_wallet_challenger.py — Empirical Verification Harness for M2 Pass 2 Edge Cases

Tests:
1. Storage upload error handling (unreachable storage bucket, corrupted file payloads).
2. Wallet update failures and partial transaction rollbacks.
"""

import os
import io
import unittest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from main import app
from database import supabase_admin
from dependencies import get_current_user

client = TestClient(app)


def create_mock_db_chain(return_data=None):
    mock_chain = MagicMock()
    mock_chain.select.return_value = mock_chain
    mock_chain.insert.return_value = mock_chain
    mock_chain.update.return_value = mock_chain
    mock_chain.delete.return_value = mock_chain
    mock_chain.eq.return_value = mock_chain
    mock_chain.or_.return_value = mock_chain
    mock_chain.in_.return_value = mock_chain
    mock_chain.order.return_value = mock_chain
    mock_chain.single.return_value = mock_chain
    mock_result = MagicMock()
    if isinstance(return_data, dict):
        mock_result.data = [return_data]
    elif return_data is not None:
        mock_result.data = return_data
    else:
        mock_result.data = []
    mock_chain.execute.return_value = mock_result
    return mock_chain


class TestStorageUploadEdgeCases(unittest.TestCase):
    """Test suite for Storage Upload edge cases in Milestone 2."""

    @patch("routers.uploads.supabase_admin")
    def test_upload_image_supabase_unreachable_exception(self, mock_supabase):
        """Verify handling when Supabase storage raises an exception during upload."""
        mock_user = MagicMock()
        mock_user.id = "vendor-123"
        app.dependency_overrides[get_current_user] = lambda: mock_user

        mock_supabase.storage.from_.return_value.upload.side_effect = Exception("Connection refused to Supabase Storage")

        file_content = b"\xFF\xD8\xFF\xE0\x00\x10JFIF"
        with patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = create_mock_db_chain({"role": "vendor"})
            response = client.post(
                "/uploads/image",
                files={"file": ("test.jpg", io.BytesIO(file_content), "image/jpeg")}
            )

        app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 502)
        self.assertIn("Storage upload failed", response.json()["detail"])

    def test_upload_corrupted_file_payload_with_valid_header(self):
        """
        Verify that a corrupted/non-image payload (e.g. PHP script or garbage bytes)
        is now REJECTED (HTTP 415) because the server validates file content via
        magic-byte inspection rather than trusting the client-supplied Content-Type.

        This test was previously documenting the vulnerability (expected 201).
        After the security fix, the correct expected response is 415.
        """
        mock_user = MagicMock()
        mock_user.id = "vendor-123"
        app.dependency_overrides[get_current_user] = lambda: mock_user

        corrupted_payload = b"<?php echo 'malicious code'; ?> corrupt binary garbage data 12345"

        with patch("routers.uploads.supabase_admin") as mock_supabase, patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = create_mock_db_chain({"role": "vendor"})
            mock_supabase.storage.from_.return_value.upload.return_value = {"path": "products/vendor-123/test.jpg"}
            
            response = client.post(
                "/uploads/image",
                files={"file": ("malicious.php", io.BytesIO(corrupted_payload), "image/jpeg")}
            )

        app.dependency_overrides.clear()
        # FIXED: magic-byte validation now rejects this payload
        self.assertEqual(response.status_code, 415)
        self.assertIn("does not match", response.json()["detail"])


class TestWalletTransactionRollbackEdgeCases(unittest.TestCase):
    """Test suite for Wallet Update failures and partial transaction rollbacks."""

    @patch("routers.wallet.supabase")
    @patch("routers.wallet.supabase_admin")
    def test_topup_wallet_partial_failure_profiles_updated_tx_logs_fail(
        self, mock_supabase_admin, mock_supabase
    ):
        """
        Empirically verify atomicity failure in topup_wallet:
        If wallet_balance update succeeds in profiles, but wallet_transactions insert fails,
        the balance remains updated without an audit log record.
        """
        mock_user = MagicMock()
        mock_user.id = "cust-456"
        app.dependency_overrides[get_current_user] = lambda: mock_user

        mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [{"wallet_balance": 1000}]

        mock_profile_table = MagicMock()
        mock_profile_update = MagicMock()
        mock_profile_table.update.return_value.eq.return_value.execute.return_value = mock_profile_update

        mock_tx_table = MagicMock()
        mock_tx_table.select.return_value.eq.return_value.execute.return_value.data = []  # idempotency check passes
        mock_tx_table.insert.return_value.execute.side_effect = Exception("DB Insert Error: table locked or constraint failed")

        def table_side_effect(table_name):
            if table_name == "profiles":
                return mock_profile_table
            elif table_name == "wallet_transactions":
                return mock_tx_table
            return MagicMock()

        mock_supabase_admin.table.side_effect = table_side_effect

        payload = {"amount_kobo": 50000, "reference": "ref-12345"}
        
        with patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = create_mock_db_chain({"role": "customer"})
            try:
                response = client.post("/wallet/topup", json=payload)
            except Exception:
                pass

        app.dependency_overrides.clear()
        mock_profile_table.update.assert_called_with({"wallet_balance": 51000})

    @patch("routers.disputes.supabase")
    @patch("routers.disputes.supabase_admin")
    def test_resolve_dispute_multi_vendor_partial_rollback(
        self, mock_supabase_admin, mock_supabase
    ):
        """
        Empirically verify partial rollback vulnerability in resolve_dispute ('Release to Vendor'):
        When resolving a dispute for an order with multiple vendors, if vendor 1 is updated,
        but vendor 2 fails, vendor 1 retains credited funds while dispute remains unresolved.
        """
        mock_admin = MagicMock()
        mock_admin.id = "admin-999"
        app.dependency_overrides[get_current_user] = lambda: mock_admin

        dispute_id = "disp-777"
        order_id = "ord-888"

        # Mock dispute record
        mock_dispute_data = {"id": dispute_id, "order_id": order_id, "status": "Under Review"}
        
        # Mock order record with 2 vendors (vendor-A, vendor-B)
        mock_order_data = {
            "id": order_id,
            "total_kobo": 20000,
            "order_items": [
                {"vendor_id": "vendor-A", "quantity": 1, "unit_price_kobo": 10000},
                {"vendor_id": "vendor-B", "quantity": 1, "unit_price_kobo": 10000},
            ]
        }

        # Mock select table calls
        def supabase_table_select(table_name):
            mock_t = MagicMock()
            if table_name == "disputes":
                mock_t.select.return_value.eq.return_value.execute.return_value.data = [mock_dispute_data]
            elif table_name == "orders":
                mock_t.select.return_value.eq.return_value.execute.return_value.data = [mock_order_data]
            elif table_name == "profiles":
                # Returns 0 balance for vendors
                mock_t.select.return_value.eq.return_value.execute.return_value.data = [{"wallet_balance": 0}]
            return mock_t

        mock_supabase.table.side_effect = supabase_table_select

        # Mock admin updates: vendor-A succeeds, vendor-B fails on update
        vendor_a_updated = []
        def admin_table_side_effect(table_name):
            mock_t = MagicMock()
            if table_name == "profiles":
                def eq_side_effect(column, val):
                    mock_eq = MagicMock()
                    if val == "vendor-A":
                        vendor_a_updated.append(val)
                        mock_eq.execute.return_value.data = [{"id": "vendor-A", "wallet_balance": 10000}]
                    elif val == "vendor-B":
                        mock_eq.execute.return_value.data = [] # Simulates update failure (returns empty data)
                    return mock_eq
                mock_t.update.return_value.eq.side_effect = eq_side_effect
            elif table_name == "wallet_transactions":
                mock_t.insert.return_value.execute.return_value.data = [{"id": "tx-1"}]
            return mock_t

        mock_supabase_admin.table.side_effect = admin_table_side_effect

        payload = {
            "resolution_outcome": "Release to Vendor",
            "resolution_notes": "Releasing funds to vendors after review."
        }

        with patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = create_mock_db_chain({"role": "admin"})
            response = client.patch(f"/disputes/{dispute_id}/resolve", json=payload)

        app.dependency_overrides.clear()
        
        # Verify result: HTTP 500 returned because vendor-B update failed
        self.assertEqual(response.status_code, 500)
        self.assertIn("Failed to update vendor vendor-B wallet balance", response.json()["detail"])
        # BUT vendor-A was ALREADY updated in profiles table!
        self.assertIn("vendor-A", vendor_a_updated)

if __name__ == "__main__":
    unittest.main()
