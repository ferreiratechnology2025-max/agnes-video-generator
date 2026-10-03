"""core.utils.r2_uploader_mock — dry-run R2 uploader.

When real R2 credentials are NOT set, the tunnel still needs to produce a
usable URL for the next scene.  This module provides a R2UploaderMock that:
- Returns deterministic dummy URLs (https://mock.i2v/frames/{key})
- Has ``upload_frame`` / ``delete_object_by_key`` / ``cleanup_all`` stubs
  that are no-ops (or log-only).
- Is instantiated by ``get_r2_uploader()`` when ``is_r2_available()`` is
  False but ``AGNES_VIMAX_I2V_TUNNEL=1`` is set and ``AGNES_MOCK_API=1``.

This lets the full sequential pipeline execute and log the complete I2V
handoff without any real cloud interaction.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Optional, Set

logger = logging.getLogger(__name__)

_DUMMY_DOMAIN = "https://mock.i2v"


class R2UploaderMock:
    """Stub uploader used when real R2 creds are absent (AGNES_MOCK_API mode)."""

    _DUMMY_DOMAIN = "https://mock.i2v"

    def __init__(self, prefix: str = "frames"):
        self.prefix = prefix
        self.public_domain = self._DUMMY_DOMAIN
        self.bucket = "mock-bucket"
        self.cleanup = True
        self._registered_keys: Set[str] = set()

    def upload_frame(self, local_path: str) -> Optional[str]:
        key = self._build_key(local_path)
        url = f"{self._DUMMY_DOMAIN}/{key}"
        self._registered_keys.add(key)
        logger.info("[R2Mock] upload_frame → %s (key=%s)", url, key)
        return url

    def delete_object_by_key(self, s3_key: str) -> None:
        if s3_key in self._registered_keys:
            self._registered_keys.discard(s3_key)
            logger.info("[R2Mock] delete_object_by_key → %s (removed)", s3_key)
        else:
            logger.debug("[R2Mock] delete_object_by_key: unknown key %s, ignored", s3_key)

    def cleanup_all(self) -> int:
        n = len(self._registered_keys)
        self._registered_keys.clear()
        logger.info("[R2Mock] cleanup_all: purged %d keys", n)
        return n

    def _build_key(self, local_path: str) -> str:
        ts = int(time.time())
        uid = uuid.uuid4().hex[:8]
        stem = os.path.splitext(os.path.basename(local_path))[0]
        return f"{self.prefix}/{ts}_{uid}_{stem}.jpg"
