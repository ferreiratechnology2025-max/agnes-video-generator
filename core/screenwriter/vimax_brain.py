"""core.screenwriter.vimax_brain — ViMax Brain: LLM storyboard engine for the
manuscript-to-video pipeline.

Inject a pre-processor that turns a raw manuscript into a *structured* JSON
storyboard (an array of scenes with explicit ``narration_text``,
``visual_description``, ``camera_motion`` and ``lighting_and_style``) instead
of the legacy character-count splitting.

The class exposes:
- ``generate_storyboard(manuscript_text, style="") -> List[dict]`` — returns
  the parsed list of scenes;
- ``to_paragraphs(scenes) -> List[ManuscriptParagraph]`` — builds the
  pipeline's paragraph objects from the JSON output, where each scene's
  ``narration_text`` is stored into ``ManuscriptParagraph.text`` (so the
  native TTS keeps working) and the visual description is preserved on the
  paragraph as ``scene_prompt``.

The LLM call goes through the existing ``AgnesChatAPI.chat_json`` method
(multi-key rotation, 429 retry, key switching already handled there).  The
system prompt forces the LLM to return a JSON object with a top-level ``scenes``
key that is an *array* of scene objects.  The response is validated before it
is returned so the pipeline can rely on the shape.
"""

import json
import logging
from typing import List, Optional

from core.api.agnes_chat import AgnesChatAPI, strip_code_fence
from models.task import ManuscriptParagraph

logger = logging.getLogger(__name__)


# Allowed camera movements (case-insensitive, but output normalised to lowercase).
_CAMERA_MOTION_ENUM = (
    "static",
    "pan",
    "tilt",
    "dolly_in",
    "dolly_out",
    "tracking",
    "handheld",
    "crane",
    "aerial",
    "whip_pan",
    "zoom_in",
    "zoom_out",
)


_SYSTEM_PROMPT = """\
You are the "ViMax Brain", a cinematic storyboard engine that converts raw
narration text into a sequence of video-ready scenes.

For EACH scene you must produce:
- "narration_text": The exact narration lines the voice-over will read for
  this scene.  The concatenation of all scenes' narration_text must equal
  the input text (no duplication, no omission, same order).
- "visual_description": A 3-5 sentence cinematic description of what is on
  screen — characters, environment, palette, composition.  Written in the
  same language as the input.  No motion words here (this is a still frame
  snapshot description).
- "camera_motion": ONE of these keywords:
  static, pan, tilt, dolly_in, dolly_out, tracking, handheld, crane, aerial,
  whip_pan, zoom_in, zoom_out.
- "lighting_and_style": One sentence describing lighting, mood and art style.
  Same language as the input.

Rules:
- Scene count between 2 and 8 depending on text length (target ~5-12s of
  narration per scene).
- Each scene's narration_text MUST be at least one full sentence.
- Do NOT add any scene that does not exist in the input.
- Do NOT add visual details that contradict the input text.
- Output MUST be a single JSON object with key "scenes" containing an array.
- Output in the SAME LANGUAGE as the input text.
- No markdown, no code fences, no commentary — just the JSON object.
"""

_USER_PROMPT_TEMPLATE = """\
<manuscript>
{manuscript}
</manuscript>

<style>
{style}
</style>

Please convert this manuscript into a storyboard of {min_scenes}-{max_scenes} scenes.
Return the JSON object now.
"""


def _detect_language_hint(text: str) -> str:
    """Return a language hint from the dominant script (best effort, no external
    library import here — keep it cheap)."""
    # Quick heuristic: Arabic block, CJK, Latin.  This is used only to bias
    # the LLM prompt; the LLM also sees the full text and will copy language.
    if any("\u0600" <= ch <= "\u06FF" for ch in text[:400]):
        return "Arabic"
    if any("\u4E00" <= ch <= "\u9FFF" for ch in text[:400]):
        return "Chinese (Simplified)"
    # Default Latin
    return "Latin script"


class ViMaxBrain:
    """LLM-backed storyboard generator (the "brain" of the ViMax architecture).

    Usage::

        brain = ViMaxBrain(api_key, model="agnes-2.0-flash")
        scenes = brain.generate_storyboard(manuscript_text, style="cinematic")
        # scenes: List[dict] each with narration_text / visual_description /
        #        camera_motion / lighting_and_style
        paragraphs = brain.to_paragraphs(scenes)
    """

    def __init__(self, api_key: str, model: str = "agnes-2.0-flash", language: str = "auto"):
        self.api_key = api_key
        self.model = model
        self.language = language
        self.chat_api = AgnesChatAPI(api_key=api_key, model=model)

    # ── public API ─────────────────────────────────────────────────────

    def generate_storyboard(
        self,
        manuscript_text: str,
        style: str = "",
        min_scenes: int = 2,
        max_scenes: int = 8,
    ) -> List[dict]:
        """Turn the raw manuscript into a structured JSON storyboard.

        Args:
            manuscript_text: The raw narration text.
            style: Optional visual style hint (injected into the prompt).
            min_scenes: Lower bound for scene count (recommended 2).
            max_scenes: Upper bound for scene count (recommended 8).

        Returns:
            A list of scene dicts, each containing the four ViMax fields.

        Raises:
            ValueError: If the LLM returned an unparseable / malformed JSON.
        """
        text = (manuscript_text or "").strip()
        if not text:
            raise ValueError("manuscript_text must be non-empty")

        lang_hint = _detect_language_hint(text)
        logger.info(
            "[ViMax] Generating storyboard (%d chars, language hint=%s, "
            "style=%r, scenes %d-%d)...",
            len(text), lang_hint, style, min_scenes, max_scenes,
        )

        user_prompt = _USER_PROMPT_TEMPLATE.format(
            manuscript=strip_code_fence(text),
            style=style.strip() or "(unspecified — use a cinematic realist style)",
            min_scenes=min_scenes,
            max_scenes=max_scenes,
        )
        # Prepend a language-pin to reduce LLM drift (similar to the existing
        # Screenwriter language pinning in the creative pipeline).
        sys = _SYSTEM_PROMPT + f"\n\nIMPORTANT: The input is {lang_hint}. " \
            "Output the storyboard in the SAME LANGUAGE as the input text."

        raw = self.chat_api.chat(sys, user_prompt, max_tokens=4096)
        parsed = self._parse_scenes(raw, min_scenes=min_scenes, max_scenes=max_scenes)

        logger.info(
            "[ViMax] Storyboard generated: %d scenes, total narration %d chars",
            len(parsed), sum(len(s.get("narration_text", "")) for s in parsed),
        )
        return parsed

    def to_paragraphs(self, scenes: List[dict]) -> List[ManuscriptParagraph]:
        """Convert a ViMax storyboard into pipeline paragraphs.

        - ``text``       <- scene's ``narration_text``   (feeds TTS)
        - ``scene_prompt`` <- scene's ``visual_description`` +
                              ``camera_motion`` +
                              ``lighting_and_style``
                              (the "enhanced prompt" ready for the video API)
        """
        paragraphs: List[ManuscriptParagraph] = []
        for i, scene in enumerate(scenes):
            narration = (scene.get("narration_text") or "").strip()
            visual = (scene.get("visual_description") or "").strip()
            motion = (scene.get("camera_motion") or "static").strip().lower()
            lighting = (scene.get("lighting_and_style") or "").strip()

            # Normalise camera motion keyword.
            if motion not in _CAMERA_MOTION_ENUM:
                motion = "static"

            # Build the enhanced prompt in a stable, model-friendly format.
            # (This matches the Phase 2 deliverable: just concatenate the
            # structured ViMax fields — no second LLM pass.)
            enhanced_prompt = self.build_enhanced_prompt(visual, motion, lighting)

            paragraphs.append(
                ManuscriptParagraph(
                    index=i,
                    text=narration,
                    scene_prompt=enhanced_prompt,
                    same_scene_as_prev=False,
                )
            )
        logger.info(
            "[ViMax] to_paragraphs: %d paragraphs built "
            "(enhanced prompts: %s)",
            len(paragraphs),
            [p.scene_prompt[:40] for p in paragraphs],
        )
        return paragraphs

    @staticmethod
    def build_enhanced_prompt(
        visual_description: str,
        camera_motion: str,
        lighting_and_style: str,
    ) -> str:
        """Concatenate the three ViMax fields into one video-API prompt.

        Format (used for t2v API; safe for all Agnes video models):
            "<visual>. <motion>. <lighting>."

        The resulting string is passed directly to ``submit_video`` as the
        prompt — no further LLM step.
        """
        parts = [
            (visual_description or "").strip(),
            f"[Camera] {camera_motion}".strip() if camera_motion else "",
            (lighting_and_style or "").strip(),
        ]
        return " ".join(p for p in parts if p).strip()

    # ── internal helpers ───────────────────────────────────────────────

    @staticmethod
    def _parse_scenes(raw: str, min_scenes: int, max_scenes: int) -> List[dict]:
        """Parse the raw LLM reply into a validated list of scene dicts.

        Tries, in order:
          1. ``json.loads`` after ``strip_code_fence``
          2. Regex-extract first ``{...}`` block (json_repair optional)
          3. Retry via json_repair

        Raises ValueError on final failure.
        """
        cleaned = strip_code_fence(raw)

        # 1) Direct
        try:
            data = json.loads(cleaned)
            return ViMaxBrain._validate_scenes(
                data.get("scenes", []), min_scenes, max_scenes,
            )
        except ValueError:
            pass

        # 2) Regex block
        import re as _re
        m = _re.search(r"\{[\s\S]*\}", cleaned)
        if m:
            try:
                data = json.loads(m.group())
                return ViMaxBrain._validate_scenes(
                    data.get("scenes", []), min_scenes, max_scenes,
                )
            except ValueError:
                pass

        # 3) json_repair (optional)
        try:
            from json_repair import repair_json
            repaired = repair_json(cleaned, return_objects=True)
            if isinstance(repaired, dict):
                return ViMaxBrain._validate_scenes(
                    repaired.get("scenes", []), min_scenes, max_scenes,
                )
        except Exception:
            pass

        logger.error(
            "[ViMax] Failed to parse storyboard JSON. Preview: %s",
            raw[:300],
        )
        raise ValueError(
            "ViMaxBrain: LLM did not return a valid storyboard JSON object. "
            "The expected format is {\"scenes\": [{...}, ...]}."
        )

    @staticmethod
    def _validate_scenes(scenes, min_scenes: int, max_scenes: int) -> List[dict]:
        """Validate & normalise the scene list.

        - Coerce each item to a dict.
        - Drop empty / non-dict entries.
        - Fill missing fields with defaults so the pipeline can always build
          an ``enhanced_prompt``.
        - If the list is shorter than ``min_scenes`` log a warning (do NOT
          raise — better to proceed with fewer scenes than to fail the task).
        - If longer than ``max_scenes`` truncate (LLM overshoot) and log.
        """
        out: List[dict] = []
        for i, s in enumerate(scenes or []):
            if not isinstance(s, dict):
                continue
            narration = str(s.get("narration_text", "")).strip()
            if not narration:
                # Drop scenes without narration (TTS needs it).
                logger.warning(
                    "[ViMax] Scene %d has empty narration_text, dropped", i,
                )
                continue
            visual = str(s.get("visual_description", "")).strip()
            motion = str(s.get("camera_motion", "static")).strip().lower()
            if motion not in _CAMERA_MOTION_ENUM:
                motion = "static"
            lighting = str(s.get("lighting_and_style", "")).strip()
            out.append({
                "narration_text": narration,
                "visual_description": visual,
                "camera_motion": motion,
                "lighting_and_style": lighting,
            })
        if len(out) < min_scenes:
            logger.warning(
                "[ViMax] Storyboard has only %d scenes (min requested %d)",
                len(out), min_scenes,
            )
        if len(out) > max_scenes:
            logger.warning(
                "[ViMax] Truncating storyboard from %d to %d scenes",
                len(out), max_scenes,
            )
            out = out[:max_scenes]
        return out


__all__ = [
    "ViMaxBrain",
]
