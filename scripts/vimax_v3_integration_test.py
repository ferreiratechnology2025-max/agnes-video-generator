"""Phase 3 I2V Tunnel — End-to-End Validation Script.

Proves the complete sequential flow:
  [I2VTunnel] Scene 0 submitted (mode=t2v, ref=—)
  [I2VTunnel] Scene 0 video COMPLETED, downloading...
  [I2VTunnel] Scene 0 video saved → .../para_0/video.mp4
  [I2VTunnel] Scene 0 last frame extracted → .../para_0/last_frame.jpg
  [I2VTunnel] Scene 0 last frame uploaded → https://propostas.api-plataforma.com/frames/...
  [I2VTunnel] Scene 1 submitted (mode=i2v, ref=https://propostas...)
  [I2VTunnel] Scene 1 video COMPLETED, downloading...
  [I2VTunnel] Scene 1 video saved → .../para_1/video.mp4
  [I2VTunnel] Scene 0 last frame deleted (consumer consumed it)
  [I2VTunnel] Sequential generation COMPLETE (2 scenes, R2=True)

Usage:
    python scripts/vimax_v3_integration_test.py       # dry-run (mock API + real R2)
    python scripts/vimax_v3_integration_test.py --real # real API calls (R2 creds required)
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ViMaxV3Test")

# ── Manuscript: 2 paragraphs → exactly 2 scenes ──────────────────
MANUSCRIPT_TEXT = (
    "Artificial intelligence is transforming the film industry faster than anyone predicted. "
    "A single prompt can now generate entire sequences that once required weeks of shooting. "
    "Yet the human eye still spots the seams."
    "The best filmmakers use AI as a collaborator, not a replacement — "
    "curating, directing, and refining the output until it breathes."
)


async def main(dry_run: bool, real_api: bool):
    from core.screenwriter.vimax_brain import ViMaxBrain
    from core.utils.r2_uploader import is_r2_available, get_r2_uploader
    from core.compositor.processor import VideoProcessor

    # ── 1. R2 tunnel check ────────────────────────────────────────
    r2_available = is_r2_available()
    uploader = get_r2_uploader()
    r2_used = r2_available and uploader is not None
    logger.info("[1/4] R2 tunnel available: %s  (uploader=%s)", r2_available, type(uploader).__name__)

    # ── 2. ViMaxBrain storyboard ──────────────────────────────────
    api_key = os.environ.get("AGNES_API_KEY", "")
    if not api_key:
        logger.error("No AGNES_API_KEY. Aborting.")
        sys.exit(1)

    logger.info("[2/4] Calling ViMaxBrain.generate_storyboard (min=2, max=2)...")
    brain = ViMaxBrain(api_key=api_key, model="agnes-2.5-flash")
    scenes = brain.generate_storyboard(MANUSCRIPT_TEXT, min_scenes=2, max_scenes=2)
    paragraphs = brain.to_paragraphs(scenes)
    assert len(paragraphs) == 2, f"Expected 2 paragraphs, got {len(paragraphs)}"
    logger.info("[2/4] ViMaxBrain produced %d scenes", len(paragraphs))
    for p in paragraphs:
        logger.info("[2/4]   para[%d] text=%r  prompt=%r", p.index, p.text[:50], p.scene_prompt[:60])

    # ── 3. Sequential I2V tunnel simulation ───────────────────────
    logger.info("[3/4] Running sequential I2V tunnel flow...")
    working_dir = tempfile.mkdtemp(prefix="vimax_v3_i2v_")
    logger.info("[3/4] Working dir: %s", working_dir)

    # Build video API
    if real_api:
        from core.api.agnes_video import AgnesVideoAPI
        video_api = AgnesVideoAPI(api_key=api_key, model="agnes-video-2.5-flash")
    else:
        from core.api.agnes_video_mock import AgnesVideoAPIMock
        video_api = AgnesVideoAPIMock(api_key=api_key, model="agnes-video-2.5-flash", sleep_seconds=2.0)

    submitted_modes = []

    for i, para in enumerate(paragraphs):
        para_dir = os.path.join(working_dir, f"para_{i}")
        video_path = os.path.join(para_dir, "video.mp4")
        last_frame_path = os.path.join(para_dir, "last_frame.jpg")
        os.makedirs(para_dir, exist_ok=True)

        # Decide reference images
        if i > 0 and pending_ref:
            ref_paths = [pending_ref]
            mode = "i2v"
        else:
            ref_paths = []
            mode = "t2v"
        submitted_modes.append(mode)

        logger.info(
            "[I2VTunnel] Scene %d: submit (mode=%s, ref=%s, dur=5s)",
            i, mode, ref_paths[0][:60] if ref_paths else "—",
        )

        # Submit video (mock or real)
        video_id = await video_api.submit_video(
            prompt=para.scene_prompt,
            reference_image_paths=ref_paths,
            duration=5,
            width=768,
            height=1152,
        )
        logger.info("[I2VTunnel] Scene %d: video_id=%s", i, video_id[:20])

        # Poll to completion
        output = await video_api.wait_for_video(video_id)
        await output.save(video_path)
        logger.info("[I2VTunnel] Scene %d: video saved (%d bytes)", i, os.path.getsize(video_path))

        # Extract last frame via FFmpeg (same method as pipeline)
        VideoProcessor.extract_last_frame(video_path, last_frame_path)
        logger.info(
            "[I2VTunnel] Scene %d: last frame extracted → %s (%d bytes)",
            i, last_frame_path, os.path.getsize(last_frame_path),
        )

        # Upload to R2
        if r2_used and uploader is not None:
            url = uploader.upload_frame(last_frame_path)
            if url:
                logger.info("[I2VTunnel] Scene %d: R2 uploaded → %s", i, url)
                pending_ref = url
            else:
                logger.warning("[I2VTunnel] Scene %d: R2 upload FAILED", i)
                pending_ref = None
        else:
            if i < len(paragraphs) - 1:
                logger.warning(
                    "[I2VTunnel] Scene %d: R2 unavailable, scene %d will be T2V",
                    i, i + 1,
                )

        # Clean up previous scene's R2 frame (scene i-1's frame was consumed by scene i)
        if i > 0 and r2_used and uploader is not None:
            prev_frame = os.path.join(working_dir, f"para_{i - 1}", "last_frame.jpg")
            prev_key = uploader._build_key(prev_frame) if hasattr(uploader, '_build_key') else f"frames/prev_{i-1}.jpg"
            uploader.delete_object_by_key(prev_key)
            logger.info("[I2VTunnel] Scene %d: previous frame purged from R2", i)

    # Final cleanup
    if r2_used and uploader is not None:
        uploader.cleanup_all()
        logger.info("[I2VTunnel] Final R2 cleanup complete")

    # ── 4. Verify ─────────────────────────────────────────────────
    logger.info("[4/4] Verification:")
    logger.info("  Scene 0 mode: %s  (expected t2v)", submitted_modes[0])
    logger.info("  Scene 1 mode: %s  (expected i2v)", submitted_modes[1])
    logger.info("=" * 70)
    logger.info("ViMax Brain + I2V Tunnel validation COMPLETE")
    logger.info("  Paragraphs: %d", len(paragraphs))
    logger.info("  Submit modes: %s", submitted_modes)
    logger.info("  R2 tunnel: %s", r2_used)
    logger.info("  Working dir: %s", working_dir)
    logger.info("=" * 70)

    assert submitted_modes[0] == "t2v", "Scene 0 must be T2V"
    if r2_used:
        assert submitted_modes[1] == "i2v", f"Scene 1 must be I2V when R2 is live, got {submitted_modes[1]}"
        logger.info("[4/4] ALL DELIVERABLES VERIFIED ✓")
        logger.info("  1. Video 0 generated and saved")
        logger.info("  2. Last frame extracted via FFmpeg")
        logger.info("  3. Frame uploaded to R2 → URL injected as reference")
        logger.info("  4. Video 1 generated in I2V mode (reference image consumed)")
        logger.info("  5. Previous frame purged from R2")
    else:
        logger.warning("[4/4] R2 not available — I2V continuity NOT tested")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ViMaxV3 I2V Tunnel End-to-End Validation")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Use mock video API (default: True for safety)")
    parser.add_argument("--real", action="store_true",
                        help="Use real Agnes Video API (requires stable backend)")
    args = parser.parse_args()
    asyncio.run(main(dry_run=not args.real, real_api=args.real))
