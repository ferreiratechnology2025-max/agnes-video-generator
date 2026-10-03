"""Patch _generate_videos_sequential to add StateManager calls."""
path = r"D:\agnes-video-generator\core\pipelines\manuscript_video.py"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# 1) Add state_manager reference to the method (after the existing local imports)
old_local_import = '''        from core.utils.r2_uploader import get_r2_uploader
        uploader = get_r2_uploader()'''
new_local_import = '''        from core.utils.r2_uploader import get_r2_uploader
        from core.db.state_manager import get_state_manager
        uploader = get_r2_uploader()
        sm = get_state_manager() if self._state_manager else None'''
assert old_local_import in content, 'local import sentinel not found'
content = content.replace(old_local_import, new_local_import, 1)

# 2) After submit: advance scene to t2v/i2v
old_submit_log = '''                logger.info(
                    "[I2VTunnel] Scene %d: submitting (%s, ref=%s, dur=%ds)",
                    i,
                    "i2v" if reference_image_paths else "t2v",
                    reference_image_paths[0][:80] if reference_image_paths else "—",
                    para_duration,
                )'''
new_submit_log = '''                _scene_event = "scene_%d_i2v" % i if reference_image_paths else "scene_%d_t2v" % i
                if sm:
                    sm.advance(self.task_id, i, _scene_event)
                logger.info(
                    "[I2VTunnel] Scene %d: submitting (%s, ref=%s, dur=%ds)",
                    i,
                    "i2v" if reference_image_paths else "t2v",
                    reference_image_paths[0][:80] if reference_image_paths else "—",
                    para_duration,
                )'''
assert old_submit_log in content, 'submit log sentinel not found'
content = content.replace(old_submit_log, new_submit_log, 1)

# 3) After video saved: mark scene done
old_saved = '''            para.video_file = video_path
            self.task_manager.update_state(paragraphs=paragraphs)
            logger.info(
                "[I2VTunnel] Scene %d video saved → %s",
                i, video_path,
            )'''
new_saved = '''            para.video_file = video_path
            self.task_manager.update_state(paragraphs=paragraphs)
            if sm:
                sm.advance(self.task_id, i, "scene_%d_done" % i)
            logger.info(
                "[I2VTunnel] Scene %d video saved → %s",
                i, video_path,
            )'''
assert old_saved in content, 'saved sentinel not found'
content = content.replace(old_saved, new_saved, 1)

# 4) After R2 upload success: mark r2_uploaded
old_r2_ok = '''                if url:
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
                        consumed_keys[i] = key'''
new_r2_ok = '''                if url:
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
                        consumed_keys[i] = key'''
assert old_r2_ok in content, 'r2_ok sentinel not found'
content = content.replace(old_r2_ok, new_r2_ok, 1)

# 5) After final cleanup: mark completed
old_final = '''        self.task_manager.update_state(paragraphs=paragraphs)
        logger.info("[I2VTunnel] Sequential I2V generation complete (%d scenes)", total)'''
new_final = '''        self.task_manager.update_state(paragraphs=paragraphs)
        if sm:
            sm.complete(self.task_id, "")
        logger.info("[I2VTunnel] Sequential I2V generation complete (%d scenes)", total)'''
assert old_final in content, 'final sentinel not found'
content = content.replace(old_final, new_final, 1)

# 6) Add 503 suspension in the submit retry loop (after the else: raise)
old_raise = '''                        else:
                            raise
                saved_video_id = para.video_id

            # ── poll to completion ──'''
new_raise = '''                        else:
                            # Hard 503 after exhausting retries — suspend for backoff
                            logger.error(
                                "[I2VTunnel] Scene %d hard 503 after %d retries, suspending",
                                i, _SUBMIT_RETRIES,
                            )
                            if sm:
                                sm.suspend_for_503(self.task_id, delay_minutes=5)
                            raise
                saved_video_id = para.video_id

            # ── poll to completion ──'''
assert old_raise in content, 'raise sentinel not found'
content = content.replace(old_raise, new_raise, 1)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("StateManager transitions patched OK")
