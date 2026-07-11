"""
uploads.py — Image upload routes for Farmers Market API

Endpoints:
  POST  /uploads/image          — Upload a product image; returns the public Supabase Storage URL
  DELETE /uploads/image         — Delete an image from storage by its public URL

Storage strategy:
  - Files are stored in the Supabase Storage bucket defined by SUPABASE_STORAGE_BUCKET (.env).
  - The default bucket name is "product-images".
  - Each file is keyed as:  products/{vendor_id}/{uuid}.{ext}
  - The bucket must be set to PUBLIC so that the returned URL is directly embeddable
    in <img> tags without signed URL refresh logic.

Supported formats: JPEG · PNG · WebP
Max file size:     5 MB  (enforced by FastAPI / python-multipart before reaching the handler)
"""

import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from config import settings
from dependencies import require_role, get_current_user
from database import supabase_admin

router = APIRouter(prefix="/uploads", tags=["uploads"])

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BUCKET = os.getenv("SUPABASE_STORAGE_BUCKET", "product-images")
MAX_BYTES = 5 * 1024 * 1024          # 5 MB
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
MIME_TO_EXT  = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_storage_path(vendor_id: str, content_type: str) -> str:
    """Return a unique storage path for a vendor's product image."""
    ext = MIME_TO_EXT.get(content_type, "jpg")
    return f"products/{vendor_id}/{uuid.uuid4().hex}.{ext}"


def _public_url(path: str) -> str:
    """Derive the public URL for a Supabase Storage object."""
    supabase_url = settings.supabase_url.rstrip("/")
    return f"{supabase_url}/storage/v1/object/public/{BUCKET}/{path}"


def _path_from_url(public_url: str) -> str | None:
    """Extract the storage path from a public URL so we can delete it."""
    marker = f"/object/public/{BUCKET}/"
    idx = public_url.find(marker)
    if idx == -1:
        return None
    return public_url[idx + len(marker):]


# Magic-byte signatures for allowed image types.
# Each entry maps mime → list of (byte_offset, expected_bytes).
# ALL listed signatures for a mime type must match.
_MAGIC: dict[str, list[tuple[int, bytes]]] = {
    "image/jpeg": [(0, b"\xff\xd8\xff")],
    "image/png":  [(0, b"\x89PNG\r\n\x1a\n")],
    "image/webp": [(0, b"RIFF"), (8, b"WEBP")],
}


def _validate_image_bytes(data: bytes, claimed_mime: str) -> None:
    """
    Verify that *data* carries the binary file signature that matches
    *claimed_mime*.  Raises HTTP 415 if the signatures don't match.

    This blocks attackers who spoof the Content-Type header to upload
    arbitrary files (scripts, HTML, executables, etc.).
    """
    sigs = _MAGIC.get(claimed_mime)
    if not sigs:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type '{claimed_mime}'. Allowed: JPEG, PNG, WebP.",
        )
    for offset, signature in sigs:
        end = offset + len(signature)
        if len(data) < end or data[offset:end] != signature:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=(
                    f"File content does not match the declared type '{claimed_mime}'. "
                    "Please upload a valid JPEG, PNG, or WebP image."
                ),
            )


def _normalize_storage_path(raw: str) -> str:
    """
    Collapse any '..' or '.' components in *raw* to prevent path-traversal
    attacks.  Returns a forward-slash path with no leading slash.
    """
    stack: list[str] = []
    for segment in raw.replace("\\", "/").split("/"):
        if segment == "..":
            if stack:
                stack.pop()   # go up one level
        elif segment in ("", "."):
            continue          # skip empty / current-dir segments
        else:
            stack.append(segment)
    return "/".join(stack)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/image", status_code=status.HTTP_201_CREATED)
async def upload_product_image(
    file: UploadFile = File(..., description="JPEG, PNG, or WebP image — max 5 MB"),
    user=Depends(require_role(["vendor", "admin"])),
):
    """
    Upload a product image to Supabase Storage.

    - Accepts multipart/form-data with a single `file` field.
    - Returns `{ "url": "<public image URL>" }`.
    - The returned URL should be saved as `image_url` when creating or updating a product.

    **Usage example (JS fetch):**
    ```js
    const form = new FormData();
    form.append('file', fileInput.files[0]);
    const res = await fetch('/uploads/image', { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: form });
    const { url } = await res.json();
    ```
    """
    # ── Validate declared MIME type ─────────────────────────────────────────
    content_type = file.content_type or ""
    if content_type not in ALLOWED_MIME:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type '{content_type}'. Allowed: JPEG, PNG, WebP.",
        )

    try:
        # ── Read & validate size ─────────────────────────────────────────────────
        file_bytes = await file.read()
        if len(file_bytes) > MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="File exceeds the 5 MB size limit.",
            )

        # ── Validate actual file content via magic bytes (anti-spoofing) ─────────
        _validate_image_bytes(file_bytes, content_type)

        # ── Upload to Supabase Storage ──────────────────────────────────────────
        storage_path = _build_storage_path(str(user.id), content_type)
        try:
            supabase_admin.storage.from_(BUCKET).upload(
                path=storage_path,
                file=file_bytes,
                file_options={"content-type": content_type, "cache-control": "3600", "upsert": "false"},
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Storage upload failed: {exc}",
            )
    finally:
        await file.close()

    public_url = _public_url(storage_path)
    return {"url": public_url, "path": storage_path}


@router.delete("/image", status_code=status.HTTP_200_OK)
async def delete_product_image(
    url: str,
    user=Depends(require_role(["vendor", "admin"])),
):
    """
    Delete a previously uploaded product image from Supabase Storage.

    - Pass the full public URL returned by `POST /uploads/image` as the `url` query param.
    - Vendors may only delete images inside their own `products/{vendor_id}/` prefix.
    - Admins may delete any image.
    """
    raw_path = _path_from_url(url)
    if not raw_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The provided URL does not belong to this storage bucket.",
        )

    # Normalise to remove any '..' traversal sequences before ownership check
    storage_path = _normalize_storage_path(raw_path)

    # Vendors can only delete their own images
    profile_res = supabase_admin.table("profiles").select("role").eq("id", user.id).execute()
    profile_data = profile_res.data[0] if profile_res.data else None
    role = profile_data.get("role") if profile_data else "vendor"

    if role == "vendor":
        expected_prefix = f"products/{user.id}/"
        if not storage_path.startswith(expected_prefix):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only delete your own product images.",
            )

    try:
        supabase_admin.storage.from_(BUCKET).remove([storage_path])
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Storage deletion failed: {exc}",
        )

    return {"message": "Image deleted successfully.", "path": storage_path}
