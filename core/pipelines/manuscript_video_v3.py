"""core.pipelines.manuscript_video_v3.py (or patch) — Phase 3 I2V Tunnel.

This module is NOT a standalone pipeline class. It contains two things the
sequential I2V generation flow needs:

1.  ``extract_last_frame_to_jpg(video_path, output_jpg_path, fps=24)`` —
    wraps ``VideoProcessor.extract_last_frame`` with FFmpeg's actual last
    frame (``-sseof -1``) and returns the path.  This is the "frame N end"
    that becomes the reference for scene N+1.

2.  ``generate_videos_sequential(
        pipeline, paragraphs, video_api, working_dir,
        use_r2_tunnel=True,
    )`` — a replacement for the legacy ``_generate_videos`` that runs
    strictly sequentially:
        - Scene 0: T2V (no reference image)
        - After scene N download: extract last frame → upload R2 →
          URL becomes reference_image_paths[0] for scene N+1 (I2V mode)
        - On scene N+1 ``completed``: if AGNES_R2_CLEANUP=1, delete the R2
          key immediately (the reference has already been consumed).

    All log lines are tagged ``[I2VTunnel]`` for easy grep in the test
    deliverable.

The function accepts the pipeline as its first argument so it can reuse
``pipeline.video_api`` (the AgnesVideoAPI instance already constructed with
the correct model + key ring + rate limiter) and ``pipeline.working_dir``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
from typing import List, Optional

from core.api.agnes_video import AgnesVideoAPI, VideoTaskCancelled
from core.compositor.processor import VideoProcessor
from core.utils.r2_uploader import get_r2_uploader, is_r2_available
from models.task import ManuscriptParagraph
from core.audio.voices import duration_len, estimate_chars_per_sec

logger = logging.getLogger(__name__)

_SUBMIT_RETRIES = 3
_WAIT_RETRIES = 3
_SUBMIT_RETRY_INTERVAL_BASE_SECONDS = 30
_WAIT_RETRY_INTERVAL_BASE_SECONDS = 30


# ── helpers ──────────────────────────────────────────────────────────

def extract_last_frame_to_jpg(video_path: str, output_jpg_path: str) -> str:
    """Extract the last frame of a video to .jpg via FFmpeg -sseof -1.

    Thin wrapper over ``VideoProcessor.extract_last_frame`` (which already
    uses FFmpeg with the right flags) but logs at [I2VTunnel] level so the
    Phase 3 audit trail is greppable.

    Args:
        video_path: source .mp4
        output_jpg_path: target .jpg (parent dirs are auto-created)

    Returns:
        output_jpg_path on success; raises on failure.
    """
    logger.info(
        "[I2VTunnel] Extracting last frame: %s → %s",
        video_path, output_jpg_path,
    )
    os.makedirs(os.path.dirname(output_jpg_path) or ".", exist_ok=True)
    result = VideoProcessor.extract_last_frame(video_path, output_jpg_path)
    if not (os.path.exists(result) and os.path.getsize(result) > 0):
        raise RuntimeError(f"Last-frame extraction produced no file: {result}")
    logger.info(
        "[I2VTunnel] Last frame extracted: %s (%d bytes)",
        result, os.path.getsize(result),
    )
    return result


def _load_task_json(parallel_dir: str) -> Optional[str]:
    """Read a previously-saved video_id from task.json (resume support)."""
    p = os.path.join(parallel_dir, "task.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("video_id")
    except Exception:
        return None


def _save_task_json(parallel_dir: str, payload: dict) -> None:
    """Persist a video_id so a resumed task can re-poll instead of resubmit."""
    os.makedirs(parallel_dir, exist_ok=True)
    p = os.path.join(parallel_dir, "task.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ── main entry point ────────────────────────────────────────────────

async def generate_videos_sequential(
    pipeline,
    paragraphs: List[ManuscriptParagraph],
    *,
    use_r2_tunnel: Optional[bool] = None,  # None → auto-detect
) -> None:
    """Run the I2V tunnel sequentially over ``paragraphs``.

    For each paragraph in order:
      - submit its video (with ``reference_image_paths=[r2_url]`` when this
        is not the first scene AND the R2 tunnel is enabled and available),
      - poll to completion,
      - download to ``para_{index}/video.mp4``,
      - when the download succeeds:
            * extract the last frame to ``para_{index}/last_frame.jpg``,
            * (if R2 enabled) upload that frame to R2, register the key,
              and stash the URL in a pending reference for scene N+1.
      - when scene N+1 reaches ``completed``, delete the R2 key from scene N
        (this is the "consumed → purge" rule from the user's spec).

    After the last scene, optionally run ``uploader.cleanup_all()`` if any
    frames remain registered (e.g. the last scene's frame is never consumed
    as a reference by the next scene).

    Raises:
        VideoTaskCancelled: if the user stops the task mid-flight.
        Exception: any permanent failure (polling timeout, HTTP 4xx, etc.)
    """
    video_api: AgnesVideoAPI = pipeline.video_api
    working_dir: str = pipeline.working_dir
    width = getattr(pipeline, "_state").video_width
    height = getattr(pipeline, "_state").video_height

    # Resolve the R2 tunnel switch: explicit override wins; else auto-detect
    if use_r2_tunnel is None:
        use_r2_tunnel = is_r2_available()
    uploader = get_r2_uploader() if use_r2_tunnel else None

    if use_r2_tunnel and uploader is None:
        logger.warning(
            "[I2VTunnel] AGNES_R2_* requested but credentials missing; "
            "falling back to T2V-only (continuity will be degraded)."
        )
        use_r2_tunnel = False

    logger.info(
        "[I2VTunnel] Starting sequential generation of %d scenes (R2=%s)",
        len(paragraphs), use_r2_tunnel,
    )

    pending_ref_for_next: Optional[str] = None  # URL to inject into scene N+1
    consumed_keys: dict[int, str] = {}         # scene_index → R2 key of its frame

    for i, para in enumerate(paragraphs):
        # ── shutdown / stop check ──
        pipeline._check_shutdown()

        para_dir = os.path.join(working_dir, f"para_{para.index}")
        video_path = os.path.join(para_dir, "video.mp4")
        last_frame_path = os.path.join(para_dir, "last_frame.jpg")

        # ── resume: if video already exists, skip straight to frame extract ──
        if os.path.exists(video_path):
            para.video_file = video_path
            logger.info(
                "[I2VTunnel] Scene %d already has video at %s, reusing",
                i, video_path,
            )
            # Still need to extract the last frame so the next scene can use it.
            extract_last_frame_to_jpg(video_path, last_frame_path)
            if use_r2_tunnel and uploader is not None:
                url = uploader.upload_frame(last_frame_path)
                if url:
                    consumed_keys[para.index] = _derive_r2_key_from_url(url, uploader)
                pending_ref_for_next = url
                if not para.index + 1 < len(paragraphs):
                    # last scene: no consumer — mark for cleanup at the end
                    logger.info(
                        "[I2VTunnel] Last frame of last scene (no consumer), "
                        "will be purged at cleanup_all."
                    )
            # Pop any consumed reference from the previous scene if we resumed
            # and that R2 key has already been deleted.
            prev_idx = para.index - 1
            if prev_idx in consumed_keys:
                consumed_keys.pop(prev_idx, None)
            continue

        # ── fresh path: submit video ──
        if not para.scene_prompt:
            logger.warning(
                "[I2VTunnel] Scene %d has no scene_prompt, skipping", i,
            )
            continue

        os.makedirs(para_dir, exist_ok=True)

        # Decide reference images for THIS scene.
        # Scene 0 → empty (T2V).
        # Scene N>0 → [pending_ref_for_next] if tunnel is live.
        reference_image_paths: List[str] = []
        if i > 0 and pending_ref_for_next:
            reference_image_paths.append(pending_ref_for_next)

        para_duration = max(
            int(math.ceil(duration_len(para.text) / estimate_chars_per_sec(para.text))),
            3,
        )

        logger.info(
            "[I2VTunnel] Submitting scene %d (mode=%s, ref=%s, duration=%ds)",
            i,
            "i2v" if reference_image_paths else "t2v",
            reference_image_paths[0] if reference_image_paths else "—",
            para_duration,
        )

        video_id: Optional[str] = None
        for retry in range(_SUBMIT_RETRIES):
            try:
                video_id = await video_api.submit_video(
                    prompt=para.scene_prompt,
                    reference_image_paths=reference_image_paths,
                    duration=para_duration,
                    width=width,
                    height=height,
                )
                para.video_id = video_id
                _save_task_json(para_dir, {"video_id": video_id})
                logger.info(
                    "[I2VTunnel] Scene %d submitted, video_id=%s",
                    i, (video_id or "")[:16],
                )
                break
            except Exception as e:
                if retry < _SUBMIT_RETRIES - 1:
                    delay = _SUBMIT_RETRY_INTERVAL_BASE_SECONDS * (retry + 1)
                    logger.warning(
                        "[I2VTunnel] Scene %d submit failed (%s), "
                        "retry %d/%d in %ds...",
                        i, e, retry + 1, _SUBMIT_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise

        if not video_id:
            logger.error("[I2VTunnel] Scene %d submit exhausted retries, aborting", i)
            raise RuntimeError(
                f"I2VTunnel: scene {i} submit exhausted retries"
            )

        # ── poll to completion ──
        for retry in range(_WAIT_RETRIES):
            try:
                output = await video_api.wait_for_video(video_id)
                logger.info(
                    "[I2VTunnel] Scene %d video COMPLETED, downloading...",
                    i,
                )
                await output.save(video_path)
                break
            except VideoTaskCancelled:
                raise
            except Exception as e:
                if retry < _WAIT_RETRIES - 1:
                    delay = _WAIT_RETRY_INTERVAL_BASE_SECONDS * (retry + 1)
                    logger.warning(
                        "[I2VTunnel] Scene %d wait failed (%s), retry %d/%d "
                        "in %ds...",
                        i, e, retry + 1, _WAIT_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise

        para.video_file = video_path
        logger.info(
            "[I2VTunnel] Scene %d video saved → %s (video_id=%s)",
            i, video_path, (video_id or "")[:16],
        )

        # ── extract last frame + optionally upload to R2 ──
        extract_last_frame_to_jpg(video_path, last_frame_path)
        pending_ref_for_next: Optional[str] = None
        if use_r2_tunnel and uploader is not None:
            url = uploader.upload_frame(last_frame_path)
            if url:
                logger.info(
                    "[I2VTunnel] Scene %d last frame uploaded → %s "
                    "(will be injected into scene %d)",
                    i, url, i + 1,
                )
                pending_ref_for_next = url
                key = _derive_r2_key_from_url(url, uploader)
                consumed_keys[i] = key
            else:
                logger.warning(
                    "[I2VTunnel] Scene %d R2 upload failed; "
                    "scene %d will fall back to T2V.",
                    i, i + 1,
                )

        # ── if the PREVIOUS scene has already been consumed by this scene,
        #    and AGNES_R2_CLEANUP=1, delete the previous R2 key NOW. ──
        #    The Agnes API has already downloaded the reference by the time
        #    this scene reached ``completed``.
        if i > 0 and use_r2_tunnel and uploader is not None:
            prev_idx = i - 1
            prev_key = consumed_keys.pop(prev_idx, None)
            if prev_key and uploader.cleanup:
                uploader.delete_object(
                    os.path.join(working_dir, f"para_{prev_idx}", "last_frame.jpg")
                )

    # ── cleanup loop for any dangling keys (e.g. the last scene's frame) ──
    if use_r2_tunnel and uploader is not None:
        # Mark every remaining key as "consume" and purge in one shot.
        uploader.cleanup_all()

    logger.info(
        "[I2VTunnel] Sequential generation COMPLETE (%d scenes, R2=%s)",
        len(paragraphs), use_r2_tunnel,
    )


def _derive_r2_key_from_url(public_url: str, uploader) -> str:
    """Invert a public URL back to the S3 object key (strip the domain prefix)."""
    if not public_url.startswith(uploader.public_domain):
        return ""
    key = public_url[len(uploader.public_domain):].lstrip("/")
    return key
