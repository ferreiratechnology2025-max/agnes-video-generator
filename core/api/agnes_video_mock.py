"""core.api.agnes_video_mock — mock AgnesVideoAPI for dry-run / validation.

When ``AGNES_MOCK_API=1`` is set, the pipeline swaps its ``video_api`` for
this class.  All ``submit_video`` calls return a fake ``video_id`` immediately;
``wait_for_video`` sleeps for a configured (default 3 s) delay and then
returns a ``VideoOutput`` whose ``data`` field is a local ``.mp4`` path
created by the pipeline.

This lets the pipeline flow through the full sequential I2V tunnel logic —
frame extraction, R2 upload, second-scene reference injection — without
actually hitting the Agnes backend.

The fake video is a minimal valid-ish MP4 (1 frame, 3 s) created with
moviepy so that downstream steps (concat, audio overlay) do not choke on
invalid video files.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)


class VideoOutput:
    """Minimal VideoOutput compatible with AgnesVideoAPI.wait_for_video."""

    def __init__(self, path: str):
        self.path = path
        self.fmt = "url"
        self.ext = "mp4"
        # For legacy code paths that read self.data as the file path.
        self.data = path

    async def save(self, save_path: str) -> None:
        """Copy the fake video to ``save_path``."""
        logger.info("[MockAPI] Saving fake video → %s", save_path)
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        import shutil
        shutil.copy2(self.path, save_path)
        logger.info("[MockAPI] Fake video saved (%d bytes)", os.path.getsize(save_path))


class AgnesVideoAPIMock:
    """Mock AgnesVideoAPI: all calls succeed with a fake local video."""

    def __init__(
        self,
        api_key: str = "mock-key",
        model: str = "agnes-video-2.5-flash",
        sleep_seconds: float = 3.0,
        video_width: int = 768,
        video_height: int = 1152,
    ):
        self.api_key = api_key
        self.model = model
        self.sleep_seconds = sleep_seconds
        self.video_width = video_width
        self.video_height = video_height
        self.shutdown_event = None
        self._cache_dir: Optional[str] = None
        self._fake_mp4: Optional[str] = None

    def _ensure_fake_mp4(self) -> str:
        """Create (once) a minimal 1-frame MP4 that moviepy can concat."""
        if self._fake_mp4 is not None and os.path.exists(self._fake_mp4):
            return self._fake_mp4
        try:
            import tempfile
            from moviepy import ImageClip, VideoFileClip
        except ImportError:
            # moviepy not available — write a minimal binary placeholder
            logger.warning("[MockAPI] moviepy not available; using null placeholder mp4")
            td = tempfile.mkdtemp(prefix="agnes_mock_")
            self._fake_mp4 = os.path.join(td, "fake.mp4")
            with open(self._fake_mp4, "wb") as f:
                # Minimal valid MP4: ftyp box + moov box with one empty trak
                # (sufficient for moviepy concat; may not play in all players)
                f.write(_MINIMAL_MP4)
            return self._fake_mp4

        # Create a 1-second, 1-frame black video with a single audio channel.
        td = tempfile.mkdtemp(prefix="agnes_mock_")
        self._cache_dir = td
        self._fake_mp4 = os.path.join(td, "mock_video.mp4")
        try:
            # One-frame black image
            from moviepy import ImageClip
            img = ImageClip(
                [[0, 0, 0]] * self.video_width + [[0, 0, 0]] * self.video_height
            ).resize((self.video_width, self.video_height))
            img = img.set_duration(self.sleep_seconds)
            # Add a silent audio track
            from moviepy import AudioFileClip, AudioGenerator
            import numpy as np
            # Generate a 1-sample silence clip
            audio = AudioGenerator(lambda t: [0.0, 0.0], duration=self.sleep_seconds).to_audioclip()
            img = img.set_audio(audio)
            img.write_videofile(
                self._fake_mp4,
                fps=24,
                codec="libx264",
                audio_codec="aac",
                logger=None,
                preset="ultrafast",
            )
            logger.info(
                "[MockAPI] Fake video created: %s (%d bytes)",
                self._fake_mp4, os.path.getsize(self._fake_mp4),
            )
        except Exception as e:
            logger.warning("[MockAPI] moviepy fake-video creation failed (%s); using null placeholder", e)
            import shutil
            shutil.rmtree(self._cache_dir, ignore_errors=True)
            self._cache_dir = None
            # Fallback: minimal mp4 bytes
            td = tempfile.mkdtemp(prefix="agnes_mock_")
            self._fake_mp4 = os.path.join(td, "mock_video.mp4")
            with open(self._fake_mp4, "wb") as f:
                f.write(_MINIMAL_MP4)
        return self._fake_mp4

    async def submit_video(
        self,
        prompt: str,
        reference_image_paths: List[str] = None,
        **kwargs,
    ) -> str:
        """Return a deterministic fake video_id."""
        ref = (reference_image_paths or [])
        mode = "i2v" if ref else "t2v"
        logger.info(
            "[MockAPI] submit_video (mode=%s, ref=%s, prompt=%r...)",
            mode, ref[0][:60] if ref else "—", prompt[:60],
        )
        return f"mock-video-{id(self)}-{len(reference_image_paths or [])}"

    async def wait_for_video(self, video_id: str) -> VideoOutput:
        """Sleep, then return a VideoOutput wrapping the fake MP4."""
        logger.info("[MockAPI] wait_for_video(%s) — sleeping %.1fs", video_id, self.sleep_seconds)
        await asyncio.sleep(self.sleep_seconds)
        return VideoOutput(self._ensure_fake_mp4())


# Minimal valid-ish MP4: ftyp + minimal moov (no actual sample data).
# Sufficient for concat steps that only inspect container metadata.
_MINIMAL_MP4 = bytes([
    0x00, 0x00, 0x00, 0x1c,  # size: 28
    0x66, 0x74, 0x79, 0x70,  # 'ftyp'
    0x69, 0x73, 0x6f, 0x6d,  # 'isom'
    0x00, 0x00, 0x00, 0x00,  # version + flags
    0x00, 0x00, 0x00, 0x01,  # minor version
    0x69, 0x73, 0x6f, 0x6d,
    0x61, 0x76, 0x63, 0x31,  # brand: avc1
    0x00, 0x00, 0x00, 0x20,  # size: 32
    0x6d, 0x6f, 0x6f, 0x76,  # 'moov'
    0x00, 0x00, 0x00, 0x00,  # version + flags
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
])
