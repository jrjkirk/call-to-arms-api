"""Supabase Storage helper for admin-uploaded mission images.

Backend-mediated uploads: the admin panel POSTs an image to admin.py, which
calls upload_mission_image() here to push the bytes into a public Supabase
Storage bucket and get back the object path (kept so the image can later be
deleted alongside its Mission row) and the public URL (denormalized onto the
Mission row and used as the Discord embed image).

Config (env, server-side only):
  SUPABASE_URL          - project URL, e.g. https://<ref>.supabase.co
  SUPABASE_SERVICE_KEY  - service-role key (never exposed to the browser)
  MISSION_IMAGE_BUCKET  - bucket name (default "mission-images"); must be a
                          PUBLIC bucket so the returned URL is directly
                          embeddable in Discord.

These are read at call time (not import time), so the module imports fine in
environments where storage isn't configured yet — it only raises when an
upload/delete is actually attempted.
"""
import os
import uuid

import httpx

_ALLOWED_CONTENT_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
}


def _config() -> tuple[str, str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    bucket = os.environ.get("MISSION_IMAGE_BUCKET", "mission-images")
    if not url or not key:
        raise RuntimeError(
            "Supabase Storage is not configured: set SUPABASE_URL and "
            "SUPABASE_SERVICE_KEY (and optionally MISSION_IMAGE_BUCKET)."
        )
    return url, key, bucket


def extension_for(content_type: str) -> str:
    """The file extension for an allowed image content type, or raise 422-style
    ValueError for anything unsupported. Callers should surface this as a 422."""
    ext = _ALLOWED_CONTENT_TYPES.get((content_type or "").lower())
    if ext is None:
        raise ValueError(
            f"Unsupported image type {content_type!r}. "
            f"Allowed: {', '.join(sorted(_ALLOWED_CONTENT_TYPES))}."
        )
    return ext


def upload_mission_image(
    data: bytes, content_type: str, club_id: int, system_id: int
) -> tuple[str, str]:
    """Upload image bytes to Supabase Storage. Returns (object_path, public_url).

    object_path is bucket-relative (club_id/system_id/<uuid>.<ext>); public_url
    is the directly-embeddable https URL. Raises RuntimeError on a non-2xx
    storage response so the caller can 502/500 cleanly."""
    url, key, bucket = _config()
    ext = extension_for(content_type)
    object_path = f"{club_id}/{system_id}/{uuid.uuid4().hex}.{ext}"

    resp = httpx.post(
        f"{url}/storage/v1/object/{bucket}/{object_path}",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": content_type,
            "x-upsert": "true",
        },
        content=data,
        timeout=30,
    )
    if resp.status_code >= 300:
        raise RuntimeError(
            f"Supabase Storage upload failed ({resp.status_code}): {resp.text[:300]}"
        )

    public_url = f"{url}/storage/v1/object/public/{bucket}/{object_path}"
    return object_path, public_url


def delete_mission_image(object_path: str) -> None:
    """Best-effort delete of a stored object. Swallows errors (a missing/failed
    blob delete must not block deleting the Mission row) but prints them."""
    try:
        url, key, bucket = _config()
        resp = httpx.request(
            "DELETE",
            f"{url}/storage/v1/object/{bucket}/{object_path}",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
        if resp.status_code >= 300:
            print(f"Warning: storage delete of {object_path!r} returned "
                  f"{resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"Warning: storage delete of {object_path!r} failed: {e}")
