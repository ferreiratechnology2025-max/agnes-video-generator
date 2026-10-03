"""core.utils.r2_uploader — Cloudflare R2 (S3-compatible) ephemeral upload tunnel.

Uploads a local image to an R2 bucket and returns a publicly-accessible URL.
Designed as a transient "drop-off" (not a permanent storage layer) for the
I2V continuity tunnel in the manuscript pipeline: the caller uploads a frame,
sends the URL to the next scene's video-submission payload, then the pipeline
deletes the object once the Agnes API has consumed it.

Environment variables:
    AGNES_R2_ENDPOINT_URL    e.g. https://<account-id>.r2.cloudflarestorage.com
    AGNES_R2_ACCESS_KEY      R2 access key id
    AGNES_R2_SECRET_KEY      R2 secret access key
    AGNES_R2_PUBLIC_DOMAIN   Public URL prefix (e.g. https://cdn.example.com)
    AGNES_R2_BUCKET          bucket name (default: agnes-i2v-tunnel)
    AGNES_R2_KEY_PREFIX      prefix in bucket path (default: frames)
    AGNES_R2_CLEANUP         "1" / "true" / "yes" → delete_object after use
                             (default "1" — ephemeral by design)

If ANY of the four credential vars are missing the uploader is considered
disabled and the caller must fall back to local file references (existing
pipeline behaviour).  The helper ``is_r2_available()`` exposes this check.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Optional, Set

logger = logging.getLogger(__name__)

# Credential env-var names
_ENDPOINT_VAR = "AGNES_R2_ENDPOINT_URL"
_ACCESS_VAR = "AGNES_R2_ACCESS_KEY"
_SECRET_VAR = "AGNES_R2_SECRET_KEY"
_DOMAIN_VAR = "AGNES_R2_PUBLIC_DOMAIN"
_BUCKET_VAR = "AGNES_R2_BUCKET"
_PREFIX_VAR = "AGNES_R2_KEY_PREFIX"
_CLEANUP_VAR = "AGNES_R2_CLEANUP"

# Default bucket / prefix when not configured
_DEFAULT_BUCKET = "agnes-i2v-tunnel"
_DEFAULT_PREFIX = "frames"


def _read_env(var: str, default: str = "") -> str:
    return (os.environ.get(var) or "").strip() or default


class R2Uploader:
    """Lightweight S3-compatible R2 uploader for ephemeral I2V frame URLs."""

    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        public_domain: str,
        bucket: str = _DEFAULT_BUCKET,
        prefix: str = _DEFAULT_PREFIX,
        cleanup: bool = True,
    ):
        self.endpoint_url = endpoint_url
        self.access_key = access_key
        self.secret_key = secret_key
        self.public_domain = public_domain.rstrip("/")
        self.bucket = bucket
        self.prefix = (prefix or "").strip("/")
        self.cleanup = cleanup
        self._client = None
        self._registered_keys: Set[str] = set()

    # ── public API ───────────────────────────────────────────────────

    def upload_frame(self, local_path: str) -> Optional[str]:
        """Upload a local image and return its public URL.

        Args:
            local_path: absolute path to the image file on disk.

        Returns:
            Public URL string, or None on failure (logged and suppressed so
            the caller can fall back to local paths).
        """
        if not os.path.exists(local_path):
            logger.error("[R2] Frame not found: %s", local_path)
            return None

        try:
            s3_key = self._build_key(local_path)
            public_url = f"{self.public_domain}/{s3_key}"
            self._ensure_client()
            with open(local_path, "rb") as f:
                body = f.read()
            self._client.put_object(
                Bucket=self.bucket,
                Key=s3_key,
                Body=body,
                ContentType="image/jpeg",
            )
            logger.info("[R2] Uploaded frame → %s (key=%s)", public_url, s3_key)
            self._registered_keys.add(s3_key)
            return public_url
        except Exception as e:
            logger.warning("[R2] upload_frame failed (%s): %s", local_path, e)
            return None

    def delete_object_by_key(self, s3_key: str) -> None:
        """Delete a specific S3 key.

        Logs and swallows errors (cleanup should not abort the pipeline).
        """
        if s3_key not in self._registered_keys:
            logger.debug("[R2] Key %s was never registered, skipping delete", s3_key)
            return
        try:
            self._ensure_client()
            self._client.delete_object(Bucket=self.bucket, Key=s3_key)
            self._registered_keys.discard(s3_key)
            logger.info("[R2] Deleted frame %s", s3_key)
        except Exception as e:
            logger.warning("[R2] delete_object failed for %s: %s", s3_key, e)

    def cleanup_all(self) -> int:
        """Delete all registered keys (fire-and-forget; returns count).

        Called at pipeline shutdown when AGNES_R2_CLEANUP=1.
        """
        if not self._registered_keys or not self.cleanup:
            return 0
        count = 0
        try:
            self._ensure_client()
            for key in list(self._registered_keys):
                try:
                    self._client.delete_object(Bucket=self.bucket, Key=key)
                    count += 1
                except Exception as e:
                    logger.warning("[R2] cleanup delete %s failed: %s", key, e)
            self._registered_keys.clear()
        except Exception as e:
            logger.warning("[R2] cleanup_all failed: %s", e)
        logger.info("[R2] Cleaned up %d frame objects", count)
        return count

    # ── internals ────────────────────────────────────────────────────

    def _ensure_client(self) -> None:
        if self._client is not None:
            return
        try:
            import boto3
            from botocore.config import Config
            self._client = boto3.client(
                "s3",
                region_name="auto",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 3, "mode": "adaptive"},
                ),
            )
            # Best-effort sanity check: HEAD on the bucket.
            try:
                self._client.head_bucket(Bucket=self.bucket)
            except Exception as e:
                logger.warning(
                    "[R2] head_bucket failed (bucket may not exist or need "
                    "ListBucket permission; create it first): %s", e,
                )
        except ImportError:
            self._client = None
            raise RuntimeError(
                "boto3 is not installed; cannot use R2 tunnel. "
                "Install with: pip install boto3"
            )

    def _build_key(self, local_path: str) -> str:
        ts = int(time.time())
        uid = uuid.uuid4().hex[:8]
        stem = os.path.splitext(os.path.basename(local_path))[0]
        return f"{self.prefix}/{ts}_{uid}_{stem}.jpg"


# ── module-level factory / check ───────────────────────────────────

_instance: Optional[R2Uploader] = None


def is_r2_available() -> bool:
    """Return True iff all four credential vars are present."""
    ep = _read_env(_ENDPOINT_VAR)
    ak = _read_env(_ACCESS_VAR)
    sk = _read_env(_SECRET_VAR)
    dom = _read_env(_DOMAIN_VAR)
    return bool(ep and ak and sk and dom)


def get_r2_uploader() -> Optional[R2Uploader]:
    """Return the singleton R2Uploader (None when creds are missing,
    or a mock when AGNES_MOCK_API=1 is set for dry-run validation)."""
    global _instance
    if _instance is not None:
        return _instance
    ep = _read_env(_ENDPOINT_VAR)
    ak = _read_env(_ACCESS_VAR)
    sk = _read_env(_SECRET_VAR)
    dom = _read_env(_DOMAIN_VAR)
    mock_api = _read_env('AGNES_MOCK_API', '').strip().lower() in ('1', 'true', 'yes')
    i2v_enabled = _read_env('AGNES_VIMAX_I2V_TUNNEL', '').strip().lower() in ('1', 'true', 'yes')
    if i2v_enabled and mock_api and not (ep and ak and sk and dom):
        try:
            from core.utils.r2_uploader_mock import R2UploaderMock
            mock = R2UploaderMock(prefix=_read_env(_PREFIX_VAR, _DEFAULT_PREFIX))
            logger.info("[R2] Tunnel in MOCK mode (creds absent, AGNES_MOCK_API=1)")
            _instance = mock
            return mock
        except Exception as e:
            logger.warning("[R2] Failed to instantiate mock uploader: %s", e)
    if not (ep and ak and sk and dom):
        logger.info(
            "[R2] Tunnel unavailable — credentials missing "
            "(ep=%s ak=%s sk=%s dom=%s)",
            bool(ep), bool(ak), bool(sk), bool(dom),
        )
        return None
    bucket = _read_env(_BUCKET_VAR, _DEFAULT_BUCKET)
    prefix = _read_env(_PREFIX_VAR, _DEFAULT_PREFIX)
    cleanup = _read_env(_CLEANUP_VAR, "1").strip().lower() in ("1", "true", "yes")
    _instance = R2Uploader(
        endpoint_url=ep,
        access_key=ak,
        secret_key=sk,
        public_domain=dom,
        bucket=bucket,
        prefix=prefix,
        cleanup=cleanup,
    )
    logger.info(
        "[R2] Tunnel initialised (bucket=%s, prefix=%s, cleanup=%s)",
        bucket, prefix, cleanup,
    )
    return _instance


def reset_r2_uploader() -> None:
    """Force re-initialisation (useful in tests / after env change)."""
    global _instance
    _instance = None


__all__ = [
    "R2Uploader",
    "is_r2_available",
    "get_r2_uploader",
    "reset_r2_uploader",
]
