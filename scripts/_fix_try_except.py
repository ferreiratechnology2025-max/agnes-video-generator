"""Fix the try/except indentation in _generate_videos_sequential."""
path = r"D:\agnes-video-generator\core\pipelines\manuscript_video.py"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# The except block is currently at the wrong indentation level (outside the try).
# We need to find the block between "try:" and "except Exception" and indent it.
# Strategy: find the try block, find the except block, and re-indent everything between them.

old_block = """        try:
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
        if uploader is not None:
            uploader.cleanup_all()

        self.task_manager.update_state(paragraphs=paragraphs)
        if sm:
            sm.complete(self.task_id, "")
        logger.info("[I2VTunnel] Sequential I2V generation complete (%d scenes)", total)
        except Exception as e:
            err_str = str(e).lower()
            is_503 = "503" in err_str or "server error" in err_str
            logger.error(
                "[I2VTunnel] Hard failure in sequential generation: %s (503=%s)", e, is_503,
            )
            if sm and is_503:
                sm.suspend_for_503(self.task_id, delay_minutes=5)
            elif sm:
                sm.fail(self.task_id, str(e)[:500])
            raise"""

new_block = """        try:
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
            elif sm:
                sm.fail(self.task_id, str(e)[:500])
            raise
        else:
            if uploader is not None:
                uploader.cleanup_all()
            self.task_manager.update_state(paragraphs=paragraphs)
            if sm:
                sm.complete(self.task_id, "")
            logger.info("[I2VTunnel] Sequential I2V generation complete (%d scenes)", total)"""

assert old_block in content, 'block sentinel not found'
content = content.replace(old_block, new_block, 1)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("try/except indentation fixed OK")
