"""
test_uploads_challenger.py — Comprehensive security and correctness tests for
the /uploads/image endpoint and the /admin/categories endpoint.

Test coverage:
  Upload endpoint:
    1. Valid JPEG upload is accepted and returns 201 with a URL.
    2. Valid PNG upload is accepted.
    3. Valid WebP upload is accepted.
    4. File with wrong declared Content-Type header (unsupported) is rejected 415.
    5. File that has an allowed Content-Type header but carries wrong bytes
       (header spoofing) is rejected 415.
    6. PHP script disguised as JPEG (magic-byte mismatch) is rejected 415.
    7. Empty file disguised as JPEG is rejected 415.
    8. File that exceeds the 5 MB size limit is rejected 413.
    9. Supabase storage failure returns 502.
   10. Non-vendor/admin role is rejected 403.

  Delete endpoint:
   11. Valid vendor URL is deleted successfully.
   12. Vendor cannot delete another vendor's image (403).
   13. Path traversal in URL (e.g. ../../other-vendor) is blocked (403).
   14. URL from wrong bucket is rejected 400.
   15. Admin can delete any vendor's image.

  Categories endpoint:
   16. Admin can list categories.
   17. Vendor can list categories (was previously blocked, now allowed).
   18. Customer can list categories.
   19. Unauthenticated request is rejected 403.
"""

import io
import os
import unittest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from main import app
from dependencies import get_current_user, require_role

client = TestClient(app)

# ---------------------------------------------------------------------------
# Reusable magic-byte fixtures
# ---------------------------------------------------------------------------

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 100   # valid JPEG header
PNG_BYTES  = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # valid PNG header
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 100  # valid WebP header
PHP_BYTES  = b"<?php echo 'evil'; ?>\x00" + b"\x00" * 100  # PHP script
HTML_BYTES = b"<html><script>alert(1)</script></html>" + b"\x00" * 100
EMPTY_BYTES = b""
BIG_BYTES  = b"\xff\xd8\xff" + b"A" * (5 * 1024 * 1024 + 1)  # >5 MB but valid header
PDF_BYTES  = b"%PDF-1.4\n" + b"\x00" * 100

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://test.supabase.co")


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _mock_user(uid: str, role: str):
    """Return a mock FastAPI user with the given id."""
    user = MagicMock()
    user.id = uid
    return user


def _mock_profile_chain(role: str):
    """Return a mock supabase table chain that returns the given role for profiles."""
    chain = MagicMock()
    chain.select.return_value = chain
    chain.insert.return_value = chain
    chain.update.return_value = chain
    chain.delete.return_value = chain
    chain.eq.return_value = chain
    chain.order.return_value = chain
    chain.in_.return_value = chain
    chain.execute.return_value.data = [{"role": role}]
    return chain


# ===========================================================================
# Upload tests
# ===========================================================================

class TestUploadImage(unittest.TestCase):

    # -----------------------------------------------------------------------
    # 1. Valid JPEG
    # -----------------------------------------------------------------------
    def test_01_valid_jpeg_accepted(self):
        vendor = _mock_user("vendor-111", "vendor")
        app.dependency_overrides[get_current_user] = lambda: vendor

        with patch("routers.uploads.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("vendor")
            mock_admin.table.return_value = _mock_profile_chain("vendor")
            mock_admin.storage.from_.return_value.upload.return_value = {}

            resp = client.post(
                "/uploads/image",
                files={"file": ("photo.jpg", io.BytesIO(JPEG_BYTES), "image/jpeg")},
            )
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertIn("url", resp.json())
        self.assertIn("path", resp.json())

    # -----------------------------------------------------------------------
    # 2. Valid PNG
    # -----------------------------------------------------------------------
    def test_02_valid_png_accepted(self):
        vendor = _mock_user("vendor-111", "vendor")
        app.dependency_overrides[get_current_user] = lambda: vendor

        with patch("routers.uploads.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("vendor")
            mock_admin.table.return_value = _mock_profile_chain("vendor")
            mock_admin.storage.from_.return_value.upload.return_value = {}

            resp = client.post(
                "/uploads/image",
                files={"file": ("image.png", io.BytesIO(PNG_BYTES), "image/png")},
            )
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 201, resp.text)

    # -----------------------------------------------------------------------
    # 3. Valid WebP
    # -----------------------------------------------------------------------
    def test_03_valid_webp_accepted(self):
        vendor = _mock_user("vendor-111", "vendor")
        app.dependency_overrides[get_current_user] = lambda: vendor

        with patch("routers.uploads.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("vendor")
            mock_admin.table.return_value = _mock_profile_chain("vendor")
            mock_admin.storage.from_.return_value.upload.return_value = {}

            resp = client.post(
                "/uploads/image",
                files={"file": ("image.webp", io.BytesIO(WEBP_BYTES), "image/webp")},
            )
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 201, resp.text)

    # -----------------------------------------------------------------------
    # 4. Wrong declared Content-Type (not in allowed set)
    # -----------------------------------------------------------------------
    def test_04_unsupported_content_type_rejected(self):
        vendor = _mock_user("vendor-111", "vendor")
        app.dependency_overrides[get_current_user] = lambda: vendor

        with patch("routers.uploads.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("vendor")
            mock_admin.table.return_value = _mock_profile_chain("vendor")

            resp = client.post(
                "/uploads/image",
                files={"file": ("doc.pdf", io.BytesIO(PDF_BYTES), "application/pdf")},
            )
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 415, resp.text)
        self.assertIn("Unsupported", resp.json()["detail"])

    # -----------------------------------------------------------------------
    # 5. Header spoofing: declares image/jpeg but bytes are HTML
    # -----------------------------------------------------------------------
    def test_05_html_spoofed_as_jpeg_rejected(self):
        vendor = _mock_user("vendor-111", "vendor")
        app.dependency_overrides[get_current_user] = lambda: vendor

        with patch("routers.uploads.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("vendor")
            mock_admin.table.return_value = _mock_profile_chain("vendor")

            resp = client.post(
                "/uploads/image",
                files={"file": ("evil.jpg", io.BytesIO(HTML_BYTES), "image/jpeg")},
            )
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 415, resp.text)
        self.assertIn("does not match", resp.json()["detail"])

    # -----------------------------------------------------------------------
    # 6. PHP script disguised as JPEG
    # -----------------------------------------------------------------------
    def test_06_php_script_disguised_as_jpeg_rejected(self):
        vendor = _mock_user("vendor-111", "vendor")
        app.dependency_overrides[get_current_user] = lambda: vendor

        with patch("routers.uploads.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("vendor")
            mock_admin.table.return_value = _mock_profile_chain("vendor")

            resp = client.post(
                "/uploads/image",
                files={"file": ("shell.php", io.BytesIO(PHP_BYTES), "image/jpeg")},
            )
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 415, resp.text)
        self.assertIn("does not match", resp.json()["detail"])

    # -----------------------------------------------------------------------
    # 7. Empty file disguised as JPEG
    # -----------------------------------------------------------------------
    def test_07_empty_file_rejected(self):
        vendor = _mock_user("vendor-111", "vendor")
        app.dependency_overrides[get_current_user] = lambda: vendor

        with patch("routers.uploads.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("vendor")
            mock_admin.table.return_value = _mock_profile_chain("vendor")

            resp = client.post(
                "/uploads/image",
                files={"file": ("empty.jpg", io.BytesIO(EMPTY_BYTES), "image/jpeg")},
            )
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 415, resp.text)

    # -----------------------------------------------------------------------
    # 8. File exceeds 5 MB limit
    # -----------------------------------------------------------------------
    def test_08_oversized_file_rejected(self):
        vendor = _mock_user("vendor-111", "vendor")
        app.dependency_overrides[get_current_user] = lambda: vendor

        with patch("routers.uploads.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("vendor")
            mock_admin.table.return_value = _mock_profile_chain("vendor")

            resp = client.post(
                "/uploads/image",
                files={"file": ("big.jpg", io.BytesIO(BIG_BYTES), "image/jpeg")},
            )
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 413, resp.text)

    # -----------------------------------------------------------------------
    # 9. Supabase storage failure returns 502
    # -----------------------------------------------------------------------
    def test_09_storage_failure_returns_502(self):
        vendor = _mock_user("vendor-111", "vendor")
        app.dependency_overrides[get_current_user] = lambda: vendor

        with patch("routers.uploads.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("vendor")
            mock_admin.table.return_value = _mock_profile_chain("vendor")
            mock_admin.storage.from_.return_value.upload.side_effect = Exception("network timeout")

            resp = client.post(
                "/uploads/image",
                files={"file": ("photo.jpg", io.BytesIO(JPEG_BYTES), "image/jpeg")},
            )
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 502, resp.text)
        self.assertIn("Storage upload failed", resp.json()["detail"])

    # -----------------------------------------------------------------------
    # 10. Customer role is rejected (403)
    # -----------------------------------------------------------------------
    def test_10_customer_cannot_upload(self):
        customer = _mock_user("cust-001", "customer")
        app.dependency_overrides[get_current_user] = lambda: customer

        with patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("customer")
            resp = client.post(
                "/uploads/image",
                files={"file": ("photo.jpg", io.BytesIO(JPEG_BYTES), "image/jpeg")},
            )
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 403, resp.text)


# ===========================================================================
# Delete tests
# ===========================================================================

class TestDeleteImage(unittest.TestCase):

    def _valid_url(self, vendor_id: str, filename: str = "img.jpg") -> str:
        return f"{SUPABASE_URL}/storage/v1/object/public/product-images/products/{vendor_id}/{filename}"

    # -----------------------------------------------------------------------
    # 11. Vendor can delete their own image
    # -----------------------------------------------------------------------
    def test_11_vendor_deletes_own_image(self):
        vendor = _mock_user("vendor-111", "vendor")
        app.dependency_overrides[get_current_user] = lambda: vendor

        with patch("routers.uploads.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("vendor")
            mock_admin.table.return_value = _mock_profile_chain("vendor")
            mock_admin.storage.from_.return_value.remove.return_value = {}

            resp = client.delete(
                "/uploads/image",
                params={"url": self._valid_url("vendor-111")},
            )
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)

    # -----------------------------------------------------------------------
    # 12. Vendor cannot delete another vendor's image
    # -----------------------------------------------------------------------
    def test_12_vendor_cannot_delete_others_image(self):
        vendor = _mock_user("vendor-111", "vendor")
        app.dependency_overrides[get_current_user] = lambda: vendor

        with patch("routers.uploads.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("vendor")
            mock_admin.table.return_value = _mock_profile_chain("vendor")

            # URL belongs to vendor-999
            resp = client.delete(
                "/uploads/image",
                params={"url": self._valid_url("vendor-999")},
            )
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 403, resp.text)

    # -----------------------------------------------------------------------
    # 13. Path traversal in URL is blocked
    # -----------------------------------------------------------------------
    def test_13_path_traversal_in_url_blocked(self):
        vendor = _mock_user("vendor-111", "vendor")
        app.dependency_overrides[get_current_user] = lambda: vendor

        # Try to escape into vendor-999's folder via '..'
        traversal_url = (
            f"{SUPABASE_URL}/storage/v1/object/public/product-images/"
            f"products/vendor-111/../../vendor-999/secret.jpg"
        )
        with patch("routers.uploads.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("vendor")
            mock_admin.table.return_value = _mock_profile_chain("vendor")

            resp = client.delete("/uploads/image", params={"url": traversal_url})
        app.dependency_overrides.clear()
        # After normalization the path should be products/vendor-999/secret.jpg
        # which does NOT start with products/vendor-111/ → 403
        self.assertEqual(resp.status_code, 403, resp.text)

    # -----------------------------------------------------------------------
    # 14. URL from a different bucket is rejected
    # -----------------------------------------------------------------------
    def test_14_wrong_bucket_url_rejected(self):
        vendor = _mock_user("vendor-111", "vendor")
        app.dependency_overrides[get_current_user] = lambda: vendor

        wrong_bucket_url = (
            f"{SUPABASE_URL}/storage/v1/object/public/dispute-evidence/"
            f"products/vendor-111/img.jpg"
        )
        with patch("routers.uploads.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("vendor")
            mock_admin.table.return_value = _mock_profile_chain("vendor")

            resp = client.delete("/uploads/image", params={"url": wrong_bucket_url})
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 400, resp.text)

    # -----------------------------------------------------------------------
    # 15. Admin can delete any vendor's image
    # -----------------------------------------------------------------------
    def test_15_admin_can_delete_any_image(self):
        admin = _mock_user("admin-001", "admin")
        app.dependency_overrides[get_current_user] = lambda: admin

        with patch("routers.uploads.supabase_admin") as mock_admin, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("admin")
            mock_admin.table.return_value = _mock_profile_chain("admin")
            mock_admin.storage.from_.return_value.remove.return_value = {}

            resp = client.delete(
                "/uploads/image",
                params={"url": self._valid_url("vendor-999")},
            )
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)


# ===========================================================================
# Categories endpoint tests
# ===========================================================================

class TestCategoriesAccess(unittest.TestCase):

    def _mock_categories_chain(self):
        chain = MagicMock()
        chain.select.return_value = chain
        chain.order.return_value = chain
        chain.execute.return_value.data = [{"name": "Vegetables"}, {"name": "Fruits"}]
        return chain

    # -----------------------------------------------------------------------
    # 16. Admin can list categories
    # -----------------------------------------------------------------------
    def test_16_admin_can_list_categories(self):
        admin = _mock_user("admin-001", "admin")
        app.dependency_overrides[get_current_user] = lambda: admin

        with patch("routers.admin.supabase") as mock_sp, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("admin")
            mock_sp.table.return_value = self._mock_categories_chain()

            resp = client.get("/admin/categories")
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsInstance(resp.json(), list)

    # -----------------------------------------------------------------------
    # 17. Vendor can list categories
    # -----------------------------------------------------------------------
    def test_17_vendor_can_list_categories(self):
        vendor = _mock_user("vendor-111", "vendor")
        app.dependency_overrides[get_current_user] = lambda: vendor

        with patch("routers.admin.supabase") as mock_sp, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("vendor")
            mock_sp.table.return_value = self._mock_categories_chain()

            resp = client.get("/admin/categories")
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("Vegetables", resp.json())

    # -----------------------------------------------------------------------
    # 18. Customer can list categories
    # -----------------------------------------------------------------------
    def test_18_customer_can_list_categories(self):
        customer = _mock_user("cust-001", "customer")
        app.dependency_overrides[get_current_user] = lambda: customer

        with patch("routers.admin.supabase") as mock_sp, \
             patch("dependencies.supabase") as mock_dep_sp:
            mock_dep_sp.table.return_value = _mock_profile_chain("customer")
            mock_sp.table.return_value = self._mock_categories_chain()

            resp = client.get("/admin/categories")
        app.dependency_overrides.clear()
        self.assertEqual(resp.status_code, 200, resp.text)

    # -----------------------------------------------------------------------
    # 19. Unauthenticated request is rejected
    # -----------------------------------------------------------------------
    def test_19_unauthenticated_cannot_list_categories(self):
        # No dependency override → HTTPBearer will reject the missing token
        app.dependency_overrides.clear()
        resp = client.get("/admin/categories")
        self.assertIn(resp.status_code, [401, 403], resp.text)


# ===========================================================================
# Magic-bytes unit tests (pure Python, no HTTP)
# ===========================================================================

class TestMagicBytesValidator(unittest.TestCase):
    """Unit tests for the _validate_image_bytes helper directly."""

    def setUp(self):
        from routers.uploads import _validate_image_bytes
        self._validate = _validate_image_bytes

    def test_valid_jpeg(self):
        self._validate(JPEG_BYTES, "image/jpeg")  # must not raise

    def test_valid_png(self):
        self._validate(PNG_BYTES, "image/png")

    def test_valid_webp(self):
        self._validate(WEBP_BYTES, "image/webp")

    def test_php_as_jpeg_raises(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._validate(PHP_BYTES, "image/jpeg")
        self.assertEqual(ctx.exception.status_code, 415)

    def test_html_as_png_raises(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._validate(HTML_BYTES, "image/png")
        self.assertEqual(ctx.exception.status_code, 415)

    def test_empty_as_jpeg_raises(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self._validate(EMPTY_BYTES, "image/jpeg")

    def test_unknown_mime_raises(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            self._validate(PDF_BYTES, "text/plain")
        self.assertEqual(ctx.exception.status_code, 415)


# ===========================================================================
# Path-normalisation unit tests
# ===========================================================================

class TestNormalizeStoragePath(unittest.TestCase):

    def setUp(self):
        from routers.uploads import _normalize_storage_path
        self._norm = _normalize_storage_path

    def test_clean_path_unchanged(self):
        self.assertEqual(
            self._norm("products/vendor-111/abc.jpg"),
            "products/vendor-111/abc.jpg",
        )

    def test_double_dot_pops_previous_segment(self):
        # Trace: [products, vendor-111] → '..' → [products] → '..' → [] → [vendor-999, img.jpg]
        # Two '..' pops fully empty the stack before 'vendor-999' is pushed.
        result = self._norm("products/vendor-111/../../vendor-999/img.jpg")
        self.assertEqual(result, "vendor-999/img.jpg")

    def test_leading_slash_removed(self):
        result = self._norm("/products/vendor-111/img.jpg")
        self.assertEqual(result, "products/vendor-111/img.jpg")

    def test_backslash_converted(self):
        result = self._norm(r"products\vendor-111\img.jpg")
        self.assertEqual(result, "products/vendor-111/img.jpg")

    def test_dot_segments_removed(self):
        result = self._norm("products/./vendor-111/./img.jpg")
        self.assertEqual(result, "products/vendor-111/img.jpg")


if __name__ == "__main__":
    unittest.main()
