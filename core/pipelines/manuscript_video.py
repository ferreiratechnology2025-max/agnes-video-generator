"""core.pipelines.manuscript_video -- 稿件长视频生成流水线（类型 3）

用户粘贴长文本稿件 -> 按朗读时长拆段 -> 每段生成视频 prompt -> 视频生成 -> TTS+字幕 -> 拼接。

v4.0 重构：继承 MultiScenePipeline，复用模板方法 run() 与步骤编排，
仅保留稿件特有的数据准备与视频生成逻辑（覆写 _build_scenes / _generate_videos /
_generate_audio / _generate_subtitles / _composite_final）。
"""

import asyncio
import json
import logging
import math
import os
import re
from typing import Callable, List, Optional

from core.api.agnes_video import AgnesVideoAPI, VideoTaskCancelled
from core.async_io import read_text
from core.audio.voices import duration_len, estimate_chars_per_sec
from core.compositor.concatenator import VideoConcatenator
from core.pipelines import MultiScenePipeline
from core.screenwriter import Screenwriter, is_prompt_language_explicit
from core.screenwriter.vimax_brain import ViMaxBrain
from core.db.state_manager import StateManager, get_state_manager

from core.compositor.processor import VideoProcessor
from core.utils.r2_uploader import get_r2_uploader, is_r2_available
from models.task import (
    ManuscriptParagraph,
    SceneTask,
)

logger = logging.getLogger(__name__)

# Chinese sentence-ending punctuation pattern.
_SENTENCE_END_RE = re.compile(r"(?<=[。！？])")

# 语速估算统一走 core.audio.voices（PRD 1.3a），此处不再维护重复常量：
# CJK ~4 字/秒，阿拉伯文 10.5 字符/秒（2026-08-31 实测校准），其余字母文字 ~13 字符/秒。

# Greedy-merge duration thresholds (seconds).
_MAX_SEGMENT_DURATION = 12.0
_MIN_SEGMENT_DURATION = 5.0


def split_manuscript_text(text: str) -> List[str]:
    """将长文本按朗读时长拆分为段落文本列表（纯函数，无实例状态）。

    与正式流水线共用同一实现（PRD 1.6），preview 端点也调用本函数；
    语速估算按文本主要脚本取公共实现（CJK 4 字/秒 / 字母文字 13 字符/秒），
    字符数统计剥离阿拉伯语变音符号（不产生语音时长）。

    拆分策略:
        1. 先按换行符 (``\\n``) 切分为粗段落。
        2. 每个粗段落再按中文句末标点 (``。！？``) 切分为候选句。
        3. 对候选句进行贪心合并：累积时长 <= 12s，最短 >= 5s。
        4. 短句 (< 5s) 合并到前一个段落；长句 (> 12s) 保持原样不拆分。

    Args:
        text: 待拆分的稿件文本。

    Returns:
        段落文本列表。
    """
    chars_per_sec = estimate_chars_per_sec(text)

    # Step 1: split by newline.
    raw_blocks = [b.strip() for b in text.split("\n") if b.strip()]

    # Step 2: further split each block by Chinese sentence-ending punctuation.
    candidate_sentences: List[str] = []
    for block in raw_blocks:
        parts = _SENTENCE_END_RE.split(block)
        for part in parts:
            part = part.strip()
            if part:
                candidate_sentences.append(part)

    if not candidate_sentences:
        return []

    # Step 3: greedy merge.
    merged: List[str] = []
    current_text = ""
    current_duration = 0.0

    for sentence in candidate_sentences:
        sentence_duration = duration_len(sentence) / chars_per_sec

        if not current_text:
            current_text = sentence
            current_duration = sentence_duration
            continue

        prospective_duration = current_duration + sentence_duration

        if prospective_duration <= _MAX_SEGMENT_DURATION:
            current_text += sentence
            current_duration = prospective_duration
        else:
            merged.append(current_text)
            current_text = sentence
            current_duration = sentence_duration

    if current_text:
        merged.append(current_text)

    # Step 4: post-process -- merge short trailing segments into previous.
    final_texts: List[str] = []
    for segment in merged:
        seg_duration = duration_len(segment) / chars_per_sec
        if seg_duration < _MIN_SEGMENT_DURATION and final_texts:
            final_texts[-1] += segment
        else:
            final_texts.append(segment)

    return final_texts

# 重试间隔基数（秒）：delay = 基数 * (retry + 1)
_SUBMIT_RETRY_INTERVAL_BASE_SECONDS = 15
_WAIT_RETRY_INTERVAL_BASE_SECONDS = 20

# 进度映射基数（阶段内线性插值）：progress = 起始 + 跨度 * (i / max(total, 1))
_PROGRESS_SCENE_PROMPTS_START = 0.05
_PROGRESS_SCENE_PROMPTS_SPAN = 0.10
_PROGRESS_SUBMIT_START = 0.15
_PROGRESS_SUBMIT_SPAN = 0.20
_PROGRESS_WAIT_START = 0.35
_PROGRESS_WAIT_SPAN = 0.25
_PROGRESS_AUDIO_START = 0.60
_PROGRESS_SUBTITLE_START = 0.75
_PROGRESS_CONCAT_START = 0.80

# ── ViMax Brain toggle: set AGNES_VIMAX_BRAIN=1 in .env to enable the
# structured storyboard engine.  When disabled the legacy split_manuscript_text
# path is used (backwards-compatible).
_VIMAX_BRAIN_ENABLED = (
    os.environ.get('AGNES_VIMAX_BRAIN', '').strip().lower() in ('1', 'true', 'yes')
)
# ── I2V tunnel toggle: set AGNES_VIMAX_I2V_TUNNEL=1 to chain scenes via R2.
# When enabled and R2 creds are present, each scene's last frame is uploaded to
# R2 and injected as the reference image for the next scene (strictly sequential
# generation).  Falls back to the legacy parallel path when disabled.
_VIMAX_I2V_TUNNEL_ENABLED = (
    os.environ.get('AGNES_VIMAX_I2V_TUNNEL', '').strip().lower() in ('1', 'true', 'yes')
)



class ManuscriptVideoPipeline(MultiScenePipeline):
    """稿件长视频生成流水线。

    将用户提交的长文本稿件拆分为若干段落，每个段落独立生成视频片段，
    再叠加 TTS 旁白和字幕后拼接为最终长视频。

    Pipeline steps（由 MultiScenePipeline 模板编排）:
        build_scenes -> reference_images -> video_generation
        -> audio -> subtitle -> concatenation -> watermark
    """

    def __init__(
        self,
        api_key: str,
        task_id: str,
        dir_name: str = None,
        chat_model: str = "agnes-2.0-flash",
        image_model: str = "agnes-image-2.1-flash",
        video_model: str = "agnes-video-v2.0",
        progress_callback: Optional[Callable] = None,
        shutdown_event: Optional[asyncio.Event] = None,
        webhook_url: str = None,
    ):
        super().__init__(api_key, task_id, dir_name, progress_callback, shutdown_event)
        self._webhook_url = webhook_url
        self.video_api = AgnesVideoAPI(api_key=api_key, model=video_model)
        self.video_api.shutdown_event = shutdown_event
        # PRD 1.4：Screenwriter language pinning——默认 en、尊重显式配置
        # （未显式设置 PROMPT_LANGUAGE 时固定 en，修复非中文稿件被写成中文的问题）。
        _sw_language = None if is_prompt_language_explicit() else "en"
        self.screenwriter = Screenwriter(api_key=api_key, model=chat_model, language=_sw_language)

        # State manager: persistent SQLite tracking for worker daemon resume
        self._state_manager = None
        try:
            self._state_manager = get_state_manager()
            self._state_manager.create_task(task_id, "manuscript",
                                             webhook_url=self._webhook_url or "")
        except Exception as e:
            logger.warning("[Manuscript] StateManager init failed (%s); continuing without persistence", e)

        # ViMax Brain: optional structured storyboard engine (env AGNES_VIMAX_BRAIN=1).
        self.vimax_brain: ViMaxBrain | None = None
        if _VIMAX_BRAIN_ENABLED:
            try:
                self.vimax_brain = ViMaxBrain(api_key=api_key, model=chat_model)
                logger.info("[Manuscript] ViMax Brain enabled for task %s", task_id)
            except Exception as e:
                logger.warning("[Manuscript] ViMax Brain init failed (%s); falling back to legacy split", e)

        # ViMax I2V tunnel: optional sequential chaining via R2 (AGNES_VIMAX_I2V_TUNNEL=1).
        self._i2v_tunnel_enabled = (
            _VIMAX_I2V_TUNNEL_ENABLED and is_r2_available()
        )
        if self._i2v_tunnel_enabled:
            logger.info("[Manuscript] I2V tunnel ENABLED for task %s", task_id)
        else:
            logger.info(
                "[Manuscript] I2V tunnel disabled (vimax=%s, i2v_tunnel=%s, r2_avail=%s)",
                _VIMAX_BRAIN_ENABLED, _VIMAX_I2V_TUNNEL_ENABLED, is_r2_available(),
            )
        # AGNES_MOCK_API=1: swap the real video API for a local mock
        # that generates fake MP4s so the full I2V tunnel flow can be audited
        # without hitting an unstable Agnes backend.
        # Reads from os.environ first, falls back to .env via dotenv.
        _mock_env = os.environ.get('AGNES_MOCK_API', '').strip().lower()
        if not _mock_env:
            try:
                from dotenv import dotenv_values
                _dot_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    '.env',
                )
                _mock_env = (dotenv_values(_dot_path).get('AGNES_MOCK_API', '') or '').strip().lower()
            except Exception:
                pass
        _mock_api = _mock_env in ('1', 'true', 'yes')
        if _mock_api:
            try:
                from core.api.agnes_video_mock import AgnesVideoAPIMock
                self.video_api = AgnesVideoAPIMock(
                    api_key=api_key,
                    model=video_model,
                    sleep_seconds=3.0,
                    video_width=self._state.video_width if hasattr(self, '_state') else 768,
                    video_height=self._state.video_height if hasattr(self, '_state') else 1152,
                )
                logger.info("[Manuscript] Mock API enabled (AGNES_MOCK_API=1)")
            except Exception as e:
                logger.warning("[Manuscript] Mock API init failed (%s); using real API", e)

    # ------------------------------------------------------------------
    # 模板钩子：数据来源
    # ------------------------------------------------------------------

    def _get_watermark_language_text(self) -> str:
        return self._state.manuscript_text

    async def _build_scenes(self) -> None:
        """构建场景列表：ViMax Brain storyboard -> paragraphs -> scene_prompt。

        支持 resume：若 paragraphs 已存在则复用，仅补全缺失的 scene_prompt。
        启用 AGNES_VIMAX_BRAIN=1 时通过 ViMaxBrain 生成结构化 storyboard；
        否则回退到 legacy split_manuscript_text + LLM scene prompt 路径。
        """
        # resume：若 paragraphs 已存在（如中途续传），直接复用
        if not self._state.paragraphs:
            if self.vimax_brain is not None:
                logger.info("[Manuscript] _build_scenes: using ViMax Brain storyboard")
                paragraphs = await self._build_scenes_with_vimax(self._state.manuscript_text)
                self._state.paragraphs = paragraphs
                self.task_manager.update_state(paragraphs=paragraphs)
                # ViMax 已同时产出 scene_prompt，跳过 _generate_scene_prompts
            else:
                paragraphs = self._split_text(self._state.manuscript_text)
                self._state.paragraphs = paragraphs
                self.task_manager.update_state(paragraphs=paragraphs)
        else:
            logger.info(
                "[Manuscript] _build_scenes: reuse %d existing paragraphs",
                len(self._state.paragraphs),
            )

        # 为缺失 scene_prompt 的段落生成视频描述（legacy 路径；ViMax 路径已跳过）
        if not self.vimax_brain:
            await self._generate_scene_prompts(self._state.paragraphs)

        # 逐段参考图：越界 index（超出实际拆段结果）记 warning 并忽略该图，不阻断
        if self._state.reference_images:
            valid_indices = {p.index for p in self._state.paragraphs}
            for k in list(self._state.reference_images.keys()):
                try:
                    idx = int(k)
                except (TypeError, ValueError):
                    idx = -1
                if idx not in valid_indices:
                    logger.warning(
                        "[Manuscript] reference image for paragraph %s out of "
                        "range (%d paragraphs), ignored", k, len(self._state.paragraphs),
                    )
                    del self._state.reference_images[k]
            self.task_manager.update_state(reference_images=self._state.reference_images)

        # 填充通用 scenes 列表（供模板与下游步骤引用）
        self._state.scenes = [
            SceneTask(
                index=p.index,
                scene_prompt=p.scene_prompt,
                narration_text=p.text,
                duration=max(int(math.ceil(duration_len(p.text) / estimate_chars_per_sec(p.text))), 3),
            )
            for p in self._state.paragraphs
        ]
        self.task_manager.update_state(scenes=[s.model_dump() for s in self._state.scenes])

    async def _build_scenes_with_vimax(self, text: str) -> List[ManuscriptParagraph]:
        """ViMax Brain 路径：调用 LLM 生成结构化 storyboard，转回 ManuscriptParagraph。

        Args:
            text: Raw manuscript text.

        Returns:
            List[ManuscriptParagraph] with text=narration_text and
            scene_prompt=concatenated ViMax fields.
        """
        try:
            scenes = self.vimax_brain.generate_storyboard(
                text,
                style=self._state.style or "",
                min_scenes=2,
                max_scenes=8,
            )
        except Exception as e:
            logger.warning(
                "[Manuscript] ViMaxBrain.generate_storyboard failed (%s); "
                "falling back to legacy split", e, exc_info=True,
            )
            # Graceful fallback: revert to legacy split to avoid task failure
            return self._split_text_legacy(text)

        paragraphs = self.vimax_brain.to_paragraphs(scenes)
        # Persist scenes in StateManager for worker resume
        if self._state_manager:
            for p in paragraphs:
                self._state_manager.create_scene(
                    self.task_id, p.index,
                    enhanced_prompt=p.scene_prompt or "",
                    narration_text=p.text or "",
                )
        logger.info(
            "[Manuscript] ViMaxBrain produced %d scenes from %d chars",
            len(paragraphs), len(text),
        )
        # Log each paragraph's enhanced prompt (truncated) for dry-run verification.
        for p in paragraphs:
            logger.info(
                "[Manuscript] Paragraph %d: text=%r... enhanced_prompt=%r...",
                p.index, p.text[:40], p.scene_prompt[:60],
            )
        return paragraphs

    def _split_text_legacy(self, text: str) -> List[ManuscriptParagraph]:
        """Legacy fallback: split manuscript text with the original algorithm."""
        final_texts = split_manuscript_text(text)
        if not final_texts:
            logger.warning("[Manuscript] split_text: no sentences found in text")
            return []
        chars_per_sec = estimate_chars_per_sec(text)
        paragraphs: List[ManuscriptParagraph] = []
        for idx, para_text in enumerate(final_texts):
            paragraphs.append(ManuscriptParagraph(index=idx, text=para_text))
            logger.info(
                "[Manuscript] Legacy Paragraph %d: %d chars, ~%.1fs",
                idx, len(para_text), duration_len(para_text) / chars_per_sec,
            )
        return paragraphs

    async def _build_reference_images(self) -> None:
        """稿件参考图阶段：参考图在任务创建时已上传并映射到段落，
        此步骤为空实现（作为手动模式检查点 / 模板阶段占位）。"""
        return

    # v6.0 P3 / PRD 1.7：稿件默认无参考图 → references 检查点不可暂停；
    # 引入逐段参考图（reference_images）后恢复为可暂停检查点。
    def _get_pausable_steps(self) -> set:
        from core.pipelines import _STEP_TO_CHECKPOINT

        steps = set(_STEP_TO_CHECKPOINT.keys())
        if not (self._state and getattr(self._state, "reference_images", None)):
            steps.discard("step_reference_images")
        return steps

    # ------------------------------------------------------------------
    # 步骤实现（覆写通用实现以保留稿件特有逻辑）
    # ------------------------------------------------------------------

    def _split_text(self, text: str) -> List[ManuscriptParagraph]:
        """将长文本按朗读时长拆分为段落列表。

        拆分策略:
            1. 先按换行符 (``\\n``) 切分为粗段落。
            2. 每个粗段落再按中文句末标点 (``。！？``) 切分为候选句。
            3. 对候选句进行贪心合并：累积时长 <= 12s，最短 >= 5s。
            4. 短句 (< 5s) 合并到前一个段落；长句 (> 12s) 保持原样不拆分。
        """
        # 防御性修复：检测并修复双重 UTF-8 编码
        text = self.fix_double_utf8(text)
        if text != self._state.manuscript_text:
            logger.info("[Manuscript] split_text: fixed double-encoded UTF-8 text")
            self._state.manuscript_text = text
            self.task_manager.update_state(manuscript_text=text)

        # Resume: if paragraphs already populated, return them directly.
        if self._state.paragraphs:
            logger.info(
                "[Manuscript] split_text: %d paragraphs already exist, resuming",
                len(self._state.paragraphs),
            )
            return self._state.paragraphs

        logger.info("[Manuscript] split_text: splitting %d chars...", len(text))

        # 拆分逻辑收敛为公共函数（PRD 1.6），与 preview 端点共用；
        # 语速估算与变音符号剥离统一走 core.audio.voices。
        final_texts = split_manuscript_text(text)
        if not final_texts:
            logger.warning("[Manuscript] split_text: no sentences found in text")
            return []

        chars_per_sec = estimate_chars_per_sec(text)
        # Build ManuscriptParagraph list.
        paragraphs: List[ManuscriptParagraph] = []
        for idx, para_text in enumerate(final_texts):
            paragraphs.append(ManuscriptParagraph(index=idx, text=para_text))
            logger.info(
                "[Manuscript] Paragraph %d: %d chars, ~%.1fs",
                idx, len(para_text), duration_len(para_text) / chars_per_sec,
            )

        logger.info(
            "[Manuscript] split_text: %d paragraphs created", len(paragraphs),
        )
        return paragraphs

    async def _generate_scene_prompts(
        self, paragraphs: List[ManuscriptParagraph],
    ) -> None:
        """为每个段落生成视频场景描述 prompt（语言跟随输入段落）。

        优化路线图 2.5：LLM 调用相互独立，改为有限并发（3 并发）执行；
        进度语义简化为「开始 → 完成」（避免并发下中间进度乱序回退）。
        """
        pending = [p for p in paragraphs if not p.scene_prompt]
        if pending:
            await self._emit(
                "scene_prompts", "running",
                f"生成 {len(pending)} 个场景描述...",
                _PROGRESS_SCENE_PROMPTS_START,
            )
            sem = asyncio.Semaphore(3)

            async def _gen_one(para: ManuscriptParagraph) -> None:
                async with sem:
                    self._check_shutdown()
                    logger.info(
                        "[Manuscript] scene_prompt: generating for paragraph %d...",
                        para.index,
                    )
                    prompt = await asyncio.to_thread(
                        self.screenwriter.generate_scene_prompt_for_paragraph,
                        para.text,
                        self._state.style,
                    )
                    para.scene_prompt = prompt.strip()

            await asyncio.gather(*[_gen_one(p) for p in pending])
            await self._emit(
                "scene_prompts", "completed",
                f"场景描述生成完成 ({len(pending)} 段)",
                _PROGRESS_SCENE_PROMPTS_START + _PROGRESS_SCENE_PROMPTS_SPAN,
            )
            for p in pending:
                logger.info(
                    "[Manuscript] scene_prompt %d: %s...",
                    p.index, p.scene_prompt[:80],
                )

        self.task_manager.update_state(paragraphs=paragraphs)
        self.save_prompts({
            "scene_prompts": [
                {"index": p.index, "text": p.text, "scene_prompt": p.scene_prompt}
                for p in paragraphs
            ],
        })

    async def _generate_videos(self) -> None:
        """为每个段落调用 Agnes Video API 生成视频（两阶段并行）。

        每段视频保存到 ``{working_dir}/para_{index}/video.mp4``，
        同时记录 video_id 和 curl 命令到 ``task.json`` / ``curl.sh``。

        Phase 3: when the I2V tunnel is enabled (``self._i2v_tunnel_enabled``),
        dispatches to ``_generate_videos_sequential`` instead.
        """
        if getattr(self, "_i2v_tunnel_enabled", False):
            logger.info("[I2VTunnel] Dispatching to sequential I2V flow")
            await self._generate_videos_sequential()
            return

        _SUBMIT_RETRIES = 3
        _WAIT_RETRIES = 3
        paragraphs = self._state.paragraphs
        total = len(paragraphs)

        # ── Phase 1: 批量提交 ────────────────────────────────────────────
        pending: list[tuple[int, str, str]] = []  # (para_index, video_id, video_path)

        for i, para in enumerate(paragraphs):
            self._check_shutdown()

            para_dir = os.path.join(self.working_dir, f"para_{para.index}")
            video_path = os.path.join(para_dir, "video.mp4")

            if os.path.exists(video_path):
                para.video_file = video_path
                continue

            if not para.scene_prompt:
                logger.warning(
                    "[Manuscript] video: paragraph %d has no scene_prompt, skipping",
                    para.index,
                )
                continue

            os.makedirs(para_dir, exist_ok=True)

            saved_video_id = self._load_task_json(para_dir)
            if saved_video_id:
                para.video_id = saved_video_id
                pending.append((para.index, saved_video_id, video_path))
                continue

            logger.info(
                "[Manuscript] video: submitting paragraph %d/%d...",
                i + 1, total,
            )
            await self._emit(
                "video_gen", "running",
                f"提交视频 {i + 1}/{total}",
                _PROGRESS_SUBMIT_START + _PROGRESS_SUBMIT_SPAN * (i / max(total, 1)),
            )

            para_duration = max(int(math.ceil(duration_len(para.text) / estimate_chars_per_sec(para.text))), 3)
            # 逐段参考图（PRD 1.5）：用户上传图按段落 index 映射，用于 i2v 引导画面
            ref_images = self._state.reference_images.get(str(para.index), [])

            for retry in range(_SUBMIT_RETRIES):
                try:
                    video_id = await self.video_api.submit_video(
                        prompt=para.scene_prompt,
                        reference_image_paths=ref_images,
                        duration=para_duration,
                        width=self._state.video_width,
                        height=self._state.video_height,
                    )
                    para.video_id = video_id
                    self._save_task_json(para_dir, {"video_id": video_id})
                    pending.append((para.index, video_id, video_path))
                    break
                except Exception as e:
                    if retry < _SUBMIT_RETRIES - 1:
                        delay = _SUBMIT_RETRY_INTERVAL_BASE_SECONDS * (retry + 1)
                        logger.warning(
                            "[Manuscript] video: paragraph %d submit failed "
                            "(%s), retry %d/%d in %ds...",
                            para.index, e, retry + 1, _SUBMIT_RETRIES, delay,
                        )
                        await asyncio.sleep(delay)
                    else:
                        raise

        self.task_manager.update_state(paragraphs=paragraphs)

        # ── Phase 2: 逐个等待完成 ────────────────────────────────────────
        for j, (para_idx, video_id, video_path) in enumerate(pending):
            self._check_shutdown()

            para = paragraphs[para_idx]
            await self._emit(
                "video_gen", "running",
                f"等待视频 {j + 1}/{len(pending)} ({video_id[:16]}...)",
                _PROGRESS_WAIT_START + _PROGRESS_WAIT_SPAN * (j / max(len(pending), 1)),
            )

            for retry in range(_WAIT_RETRIES):
                try:
                    video_output = await self.video_api.wait_for_video(video_id)
                    await video_output.save(video_path)
                    break
                except VideoTaskCancelled:
                    # 优化路线图 0.2：用户停止不是临时错误，不重试、直接穿透
                    raise
                except Exception as e:
                    if retry < _WAIT_RETRIES - 1:
                        delay = _WAIT_RETRY_INTERVAL_BASE_SECONDS * (retry + 1)
                        logger.warning(
                            "[Manuscript] video: paragraph %d wait failed "
                            "(%s), retry %d/%d in %ds...",
                            para_idx, e, retry + 1, _WAIT_RETRIES, delay,
                        )
                        await asyncio.sleep(delay)
                    else:
                        raise

            para.video_file = video_path
            self.task_manager.update_state(paragraphs=paragraphs)
            logger.info(
                "[Manuscript] video: paragraph %d saved → %s (video_id=%s)",
                para_idx, video_path, video_id[:16],
            )

    async def _generate_videos_sequential(self) -> None:
        """Phase 3: strictly-sequential I2V generation with R2 tunnel.

        Flow:
            0. Scene 0 → T2V (no reference image).
            1. After scene N download: extract last frame → upload R2 →
               capture public URL as the reference for scene N+1.
            2. Scene N+1 submitted in ``reference`` mode (I2V) with that URL.
            3. When scene N+1 reaches ``completed``, the scene N R2 object is
               purged (AGNES_R2_CLEANUP=1).
            4. Repeat until the last scene; at the very end, ``cleanup_all``
               purges any dangling frames.
        """
        _SUBMIT_RETRIES = 3
        _WAIT_RETRIES = 3
        paragraphs = self._state.paragraphs
        total = len(paragraphs)

        from core.utils.r2_uploader import get_r2_uploader
        from core.db.state_manager import get_state_manager
        uploader = get_r2_uploader()
        sm = get_state_manager() if self._state_manager else None
        logger.info("[I2VTunnel] sm=%s _state_manager=%s", type(sm).__name__, type(self._state_manager).__name__)
        if uploader is None:
            logger.warning(
                "[I2VTunnel] Enabled in env but no R2 creds available; "
                "falling back to T2V-only sequential (no continuity)."
            )

        pending_ref_for_next = None
        consumed_keys: dict[int, str] = {}

        try:
            for i, para in enumerate(paragraphs):
                self._check_shutdown()

                para_dir = os.path.join(self.working_dir, f"para_{para.index}")
                video_path = os.path.join(para_dir, "video.mp4")
                last_frame_path = os.path.join(para_dir, "last_frame.jpg")

                # Resume: video already on disk → skip submit, still extract frame.
                if os.path.exists(video_path):
                    para.video_file = video_path
                    logger.info(
                        "[I2VTunnel] Scene %d video reused (%s); extracting frame",
                        i, video_path,
                    )
                    self._extract_last_frame(video_path, last_frame_path)
                    # Check for previously-stored R2 URL (resume support).
                    stored = self._load_task_json(para_dir) or {}
                    pending_ref_for_next = stored.get("r2_url")
                    logger.info(
                        "[I2VTunnel] Scene %d resume: r2_url=%s",
                        i, pending_ref_for_next,
                    )
                    continue

                if not para.scene_prompt:
                    logger.warning(
                        "[I2VTunnel] Scene %d has no scene_prompt, skipping", i,
                    )
                    continue

                os.makedirs(para_dir, exist_ok=True)

                saved_video_id = self._load_task_json(para_dir)
                if saved_video_id:
                    para.video_id = saved_video_id
                else:
                    # Decide reference images: scene 0 → [] (T2V).
                    # scene N>0 → [pending_ref_for_next] if tunnel live.
                    reference_image_paths: list = []
                    if i > 0 and pending_ref_for_next:
                        reference_image_paths.append(pending_ref_for_next)

                    para_duration = max(
                        int(math.ceil(
                            duration_len(para.text) / estimate_chars_per_sec(para.text)
                        )), 3,
                    )
                    _scene_event = "scene_%d_i2v" % i if reference_image_paths else "scene_%d_t2v" % i
                    if sm:
                        sm.advance(self.task_id, i, _scene_event)
                    logger.info(
                        "[I2VTunnel] Scene %d: submitting (%s, ref=%s, dur=%ds)",
                        i,
                        "i2v" if reference_image_paths else "t2v",
                        reference_image_paths[0][:80] if reference_image_paths else "—",
                        para_duration,
                    )
                    await self._emit(
                        "video_gen", "running",
                        f"提交视频 {i + 1}/{total}",
                        _PROGRESS_SUBMIT_START + _PROGRESS_SUBMIT_SPAN * (i / max(total, 1)),
                    )
                    for retry in range(_SUBMIT_RETRIES):
                        try:
                            video_id = await self.video_api.submit_video(
                                prompt=para.scene_prompt,
                                reference_image_paths=reference_image_paths,
                                duration=para_duration,
                                width=self._state.video_width,
                                height=self._state.video_height,
                            )
                            para.video_id = video_id
                            self._save_task_json(para_dir, {"video_id": video_id})
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
                                # Hard 503 after exhausting retries — suspend for backoff
                                logger.error(
                                    "[I2VTunnel] Scene %d hard 503 after %d retries, suspending",
                                    i, _SUBMIT_RETRIES,
                                )
                                if sm:
                                    sm.suspend_for_503(self.task_id, delay_minutes=5)
                                raise
                    saved_video_id = para.video_id

                # ── poll to completion ──
                for retry in range(_WAIT_RETRIES):
                    try:
                        output = await self.video_api.wait_for_video(saved_video_id)
                        await output.save(video_path)
                        break
                    except VideoTaskCancelled:
                        raise
                    except Exception as e:
                        if retry < _WAIT_RETRIES - 1:
                            delay = _WAIT_RETRY_INTERVAL_BASE_SECONDS * (retry + 1)
                            logger.warning(
                                "[I2VTunnel] Scene %d wait failed (%s), "
                                "retry %d/%d in %ds...",
                                i, e, retry + 1, _WAIT_RETRIES, delay,
                            )
                            await asyncio.sleep(delay)
                        else:
                            # Hard failure after exhausting wait retries — suspend
                            err_str = str(e).lower()
                            is_503 = "503" in err_str or "server error" in err_str
                            logger.error(
                                "[I2VTunnel] Scene %d hard failure after %d waits (%s), "
                                "suspending: %s",
                                i, _WAIT_RETRIES, "503" if is_503 else "other", e,
                            )
                            if sm and is_503:
                                sm.suspend_for_503(self.task_id, delay_minutes=5)
                            raise

                para.video_file = video_path
                self.task_manager.update_state(paragraphs=paragraphs)
                if sm:
                    sm.advance(self.task_id, i, "scene_%d_done" % i)
                logger.info(
                    "[I2VTunnel] Scene %d video saved → %s",
                    i, video_path,
                )

                # ── extract last frame + optionally upload to R2 ──
                self._extract_last_frame(video_path, last_frame_path)
                if uploader is not None:
                    url = uploader.upload_frame(last_frame_path)
                    if url:
                        if sm:
                            sm.advance(self.task_id, i, "r2_uploaded")
                        logger.info(
                            "[I2VTunnel] Scene %d last frame uploaded → %s "
                            "(will be injected into scene %d if it exists)",
                            i, url, i + 1,
                        )
                        key = uploader._build_key(last_frame_path)
                        self._save_task_json(
                            para_dir, {"r2_url": url, "r2_key": key},
                        )
                        if i < total - 1:
                            consumed_keys[i] = key
                    else:
                        logger.warning(
                            "[I2VTunnel] Scene %d R2 upload failed; "
                            "scene %d will fall back to T2V.",
                            i, i + 1,
                        )

                # ── purge scene i-1's R2 key now that scene i consumed it ──
                if i > 0 and uploader is not None:
                    prev_idx = i - 1
                    prev_key = consumed_keys.pop(prev_idx, None)
                    if prev_key and uploader.cleanup:
                        uploader.delete_object_by_key(prev_key)

        # ── final cleanup: purge any dangling keys (last scene's frame).
        except Exception as e:
            err_str = str(e).lower()
            is_503 = "503" in err_str or "server error" in err_str
            logger.error(
                "[I2VTunnel] Hard failure in sequential generation: %s (503=%s)", e, is_503,
            )
            if sm and is_503:
                sm.suspend_for_503(self.task_id, delay_minutes=5)
                logger.info(
                    "[I2VTunnel] Task %s suspended for 503, will retry in 5 minutes",
                    self.task_id,
                )
                return  # Do NOT re-raise — let the worker pick it up on next tick
            elif sm:
                sm.fail(self.task_id, str(e)[:500])
            raise
        else:
            if uploader is not None:
                uploader.cleanup_all()
            self.task_manager.update_state(paragraphs=paragraphs)
            if sm:
                sm.complete(self.task_id, "")
            logger.info("[I2VTunnel] Sequential I2V generation complete (%d scenes)", total)

    def _extract_last_frame(self, video_path: str, output_jpg: str) -> None:
        """FFmpeg -sseof -1 wrapper (via VideoProcessor)."""
        logger.info(
            "[I2VTunnel] Extracting last frame: %s → %s",
            video_path, output_jpg,
        )
        VideoProcessor.extract_last_frame(video_path, output_jpg)
        logger.info(
            "[I2VTunnel] Last frame extracted: %s",
            output_jpg,
        )

    async def _generate_audio(self) -> object:
        """生成整段连续 TTS 音频，返回 sub_maker 供字幕步骤。"""
        paragraphs = self._state.paragraphs
        audio_config = self._state.audio_config
        full_text = "\n\n".join(p.text for p in paragraphs if p.text)
        if not full_text:
            logger.warning("[Manuscript] audio: empty full text, skipping")
            return None

        # PRD 1.2a：tashkeel 与字幕隔离——字幕/旁白导出产物用无变音符号的 plain 文本，
        # TTS 用加 tashkeel 版本（提升阿拉伯语朗读准确度）。仅当启用 add_tashkeel 时。
        narration_plain = full_text
        narration_tts = full_text
        if audio_config.add_tashkeel:
            from core.audio.tashkeel import add_tashkeel_safe

            narration_tts = add_tashkeel_safe(narration_plain)
            if narration_tts != narration_plain:
                logger.info(
                    "[Manuscript] audio: tashkeel applied to %d chars "
                    "(plain kept for subtitles/export)", len(narration_plain),
                )

        audio_path = os.path.join(self.working_dir, "full_narration.mp3")
        # v5.x 产物规范前置：导出旁白纯文本（供外部 Agent/工具处理）——用 plain 版本
        self._save_narration_txt(narration_plain, audio_path)

        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
            self._state.combined_audio = audio_path
            logger.info("[Manuscript] audio: file already exists, skipping")
            # 续传：音频已存在则仅重采 cues，避免字幕退回 legacy 启发式
            return await self._recover_sub_maker(
                narration_tts, self._state.audio_config, self._state.subtitle_config,
                audio_path,
            )

        await self._emit(
            "audio", "running",
            f"生成整段旁白 ({len(narration_tts)} 字)...",
            _PROGRESS_AUDIO_START,
        )

        sub_maker = await self._generate_audio_with_fallback(
            output_path=audio_path,
            text=narration_tts,
            audio_config=audio_config,
            subtitle_config=self._state.subtitle_config,
            duration_sec=0.0,  # 原实现未指定时长，由引擎按文本估算
            empty_placeholder="",
        )

        self._state.combined_audio = audio_path
        self.task_manager.update_state(combined_audio=audio_path)
        logger.info("[Manuscript] audio: combined → %s", audio_path)
        return sub_maker

    async def _generate_subtitles(self, sub_maker: object = None) -> None:
        """生成整段 SRT 字幕（复用通用字幕生成逻辑）。"""
        paragraphs = self._state.paragraphs
        subtitle_config = self._state.subtitle_config
        segment_texts = [p.text for p in paragraphs if p.text]
        if not segment_texts:
            logger.warning("[Manuscript] subtitle: empty text, skipping")
            return

        segment_durations = []
        for p in paragraphs:
            dur = max(duration_len(p.text) / estimate_chars_per_sec(p.text), 2.0) if p.text else 5.0
            segment_durations.append(dur)

        await self._emit(
            "subtitle", "running",
            f"生成整段字幕 ({sum(len(t) for t in segment_texts)} 字, {len(paragraphs)} 段)...",
            _PROGRESS_SUBTITLE_START,
        )

        srt_path, styles_path = await self.generate_subtitles_common(
            segment_texts=segment_texts,
            segment_durations=segment_durations,
            subtitle_config=subtitle_config,
            sub_maker=sub_maker,
            audio_path=self._state.combined_audio or "",
            screenwriter=self.screenwriter,
            video_width=self._state.video_width,
            video_height=self._state.video_height,
        )

        if styles_path:
            self._state.subtitle_styles_path = styles_path
            self.task_manager.update_state(subtitle_styles_path=styles_path)

            try:
                prompts_path = os.path.join(self.working_dir, "prompts.json")
                existing = {}
                if os.path.exists(prompts_path):
                    existing = json.loads(await read_text(prompts_path))
                existing["subtitle_styles"] = json.loads(await read_text(styles_path))
                self.save_prompts(existing)
            except Exception:
                pass

        self._state.combined_subtitle = srt_path
        self.task_manager.update_state(combined_subtitle=srt_path)
        logger.info("[Manuscript] subtitle: combined → %s", srt_path)

    async def _composite_final(self) -> str:
        """先拼接所有段落视频，再统一叠加整段音频 + 整段字幕。"""
        paragraphs = self._state.paragraphs
        subtitle_config = self._state.subtitle_config
        output_path = os.path.join(self.working_dir, "final_video.mp4")

        if os.path.exists(output_path):
            logger.info("[Manuscript] concatenate: final video already exists, skipping")
            return output_path

        video_paths = [
            p.video_file for p in paragraphs
            if p.video_file and os.path.exists(p.video_file)
        ]
        if not video_paths:
            raise RuntimeError("[Manuscript] concatenate: no valid videos to concatenate")

        has_audio = self._state.audio_config.enabled and bool(self._state.combined_audio)
        has_subtitle = subtitle_config.enabled and bool(self._state.combined_subtitle)

        styles_path = self._state.subtitle_styles_path or ""
        if styles_path and not os.path.exists(styles_path):
            styles_path = ""

        logger.info(
            "[Manuscript] concatenate: %d videos + audio=%s + subtitle=%s → %s",
            len(video_paths), has_audio, has_subtitle, output_path,
        )

        if has_audio or has_subtitle:
            await self._emit(
                "concatenate", "running",
                f"拼接 {len(video_paths)} 段视频+音频+字幕...", _PROGRESS_CONCAT_START,
            )
            await asyncio.to_thread(
                VideoConcatenator.concat_videos_with_audio_overlay,
                video_paths=video_paths,
                audio_path=self._state.combined_audio or "",
                srt_path=self._state.combined_subtitle if has_subtitle else None,
                output_path=output_path,
                subtitle_style=subtitle_config.style if has_subtitle else None,
                subtitle_styles_path=styles_path if styles_path else None,
            )
        else:
            await self._emit(
                "concatenate", "running",
                f"拼接 {len(video_paths)} 段视频（无音频字幕）...", _PROGRESS_CONCAT_START,
            )
            await asyncio.to_thread(
                VideoConcatenator.concat_videos, video_paths, output_path
            )

        logger.info("[Manuscript] concatenate: final video → %s", output_path)
        return output_path
