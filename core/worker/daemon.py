"""core.worker.daemon — asyncio worker daemon for the ViMax pipeline.

The daemon runs in a background asyncio task.  It continuously polls the
SQLite state machine for tasks that are ``pending`` or ``suspended_503`` and
whose ``next_retry_at`` has elapsed.  When it picks up a task it resumes the
pipeline from the last persisted scene, re-uploading R2 frames as needed.

If the Agnes Video API returns a hard 503 after all retries, the worker
suspends the task for a configurable backoff (default 30 minutes) instead of
burning CPU in a tight retry loop.

When a task completes, an optional webhook POST is fired with the final video
URL so the client doesn't need to keep polling.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from typing import List, Optional

# ── JSON structured log formatter ─────────────────────────────────────────────
class JsonFormatter(logging.Formatter):
    """Structured JSON log formatter for production telemetry."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj, ensure_ascii=False)


# Configure structured logging for daemon module
_logger = logging.getLogger(__name__)
_json_handler = logging.StreamHandler(sys.stdout)
_json_handler.setFormatter(JsonFormatter())
_json_handler.setLevel(logging.INFO)
# Add JSON handler if not already present
if not any(isinstance(h, JsonFormatter) for h in _logger.handlers):
    _logger.addHandler(_json_handler)
    # Keep default text handler for backward compat but set to WARNING+
    _logger.setLevel(logging.INFO)

logger = _logger

# ── configurable backoff ──────────────────────────────────────────────────────
_SUSPEND_503_MINUTES = int(os.environ.get("AGNES_WORKER_503_SUSPEND_MIN", "30"))
_POLL_INTERVAL_SECONDS = int(os.environ.get("AGNES_WORKER_POLL_INTERVAL", "5"))
_MAX_CONCURRENT_TASKS = int(os.environ.get("AGNES_WORKER_MAX_CONCURRENT", "3"))
_WEBHOOK_TIMEOUT_SECONDS = int(os.environ.get("AGNES_WORKER_WEBHOOK_TIMEOUT", "10"))


class WorkerDaemon:
    """Background asyncio task that drives the ViMax pipeline from SQLite."""

    def __init__(
        self,
        state_manager,
        pipeline_factory,
        webhook_base_url: str = "",
    ):
        self.sm = state_manager
        self._pipeline_factory = pipeline_factory  # callable(task_id) -> pipeline
        self._webhook_base = webhook_base_url
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._active_tasks: set = set()
        self._shutdown_event: Optional[asyncio.Event] = None

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop = None) -> None:
        """Start the daemon loop in the given event loop (or asyncio.get_event_loop()).
        Also registers signal handlers for graceful shutdown.
        """
        if self._running:
            return
        self._running = True
        self._shutdown_event = asyncio.Event()
        
        # Register signal handlers for graceful shutdown
        evt = loop or asyncio.get_event_loop()
        try:
            # Only register on main thread and main loop
            if evt == asyncio.get_event_loop():
                original_handlers = {}
                for sig in (signal.SIGINT, signal.SIGTERM):
                    try:
                        original_handlers[sig] = signal.getsignal(sig)
                        signal.signal(sig, self._handle_signal)
                    except (OSError, ValueError):
                        pass  # Signal not available on this platform/thread
                
                # Store original handlers for cleanup
                self._original_handlers = original_handlers
        except (RuntimeError, AttributeError):
            pass  # Not in main thread, skip signal registration
        
        self._task = evt.create_task(self._run_loop())
        logger.info(
            "[WorkerDaemon] Started (poll=%ds, suspend_503=%dm, max_concurrent=%d)",
            _POLL_INTERVAL_SECONDS, _SUSPEND_503_MINUTES, _MAX_CONCURRENT_TASKS,
        )

    def _handle_signal(self, signum, frame):
        """Signal handler: initiate graceful shutdown."""
        sig_name = signal.Signals(signum).name
        logger.info("[WorkerDaemon] Received %s, initiating graceful shutdown...", sig_name)
        self.stop()

    def stop(self) -> None:
        """Initiate graceful shutdown: stop accepting new tasks, wait for active tasks to complete."""
        if not self._running:
            return
        self._running = False
        
        # Signal all active pipelines to stop
        for task_id in list(self._active_tasks):
            logger.info("[WorkerDaemon] Requesting pipeline %s to stop...", task_id)
            # Try to get pipeline from active_pipelines if available
            try:
                from web.app_state import active_pipelines
                pipeline = active_pipelines.get(task_id)
                if pipeline and hasattr(pipeline, 'stop'):
                    pipeline.stop()
            except Exception as e:
                logger.debug("[WorkerDaemon] Could not stop pipeline %s: %s", task_id, e)
        
        # Set shutdown event for pipelines to detect
        if self._shutdown_event:
            self._shutdown_event.set()
        
        logger.info("[WorkerDaemon] Waiting for %d active task(s) to complete...", len(self._active_tasks))

    async def _run_loop(self) -> None:
        logger.info("[WorkerDaemon] Loop started")
        try:
            while self._running:
                try:
                    await asyncio.wait_for(self._tick(), timeout=_POLL_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    pass  # Normal poll interval
                except Exception as e:
                    logger.error("[WorkerDaemon] tick failed: %s", e, exc_info=True)
                
                # Check if we should shutdown
                if not self._running:
                    logger.info("[WorkerDaemon] Shutdown signal received, waiting for active tasks...")
                    break
                
                await asyncio.sleep(0.1)  # Small delay to prevent tight loop
        finally:
            # Graceful cleanup
            logger.info("[WorkerDaemon] Cleaning up...")
            await self._cleanup()
            logger.info("[WorkerDaemon] Shutdown complete (exit code 0)")

    async def _cleanup(self) -> None:
        """Cleanup resources: close DB connection, cancel tasks."""
        # Wait for active tasks to finish (with timeout)
        if self._active_tasks:
            logger.info("[WorkerDaemon] Waiting %ds for %d active task(s)...", 
                       _POLL_INTERVAL_SECONDS * 2, len(self._active_tasks))
            # Give active tasks time to complete
            for _ in range(_POLL_INTERVAL_SECONDS * 2):
                if not self._active_tasks:
                    break
                await asyncio.sleep(1)
        
        # Cancel the main loop task if still running
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        
        # Close database connection
        try:
            if hasattr(self.sm, 'close'):
                self.sm.close()
                logger.info("[WorkerDaemon] Database connection closed")
        except Exception as e:
            logger.warning("[WorkerDaemon] Error closing DB: %s", e)
        
        # Restore original signal handlers
        try:
            if hasattr(self, '_original_handlers'):
                for sig, handler in self._original_handlers.items():
                    try:
                        signal.signal(sig, handler)
                    except (OSError, ValueError):
                        pass
        except Exception:
            pass

    async def _tick(self) -> None:
        now = time.time()
        # Prune completed/failed tasks that are older than 1 hour
        self._prune_old_tasks(now)

        # Pick up tasks that are ready
        candidates = self.sm.get_pending_or_suspended(now)
        slots = _MAX_CONCURRENT_TASKS - len(self._active_tasks)
        if slots <= 0:
            return

        picked = candidates[:slots]
        for task_rec in picked:
            # Skip if already active
            if task_rec.task_id in self._active_tasks:
                continue
            if task_rec.status == "suspended_503":
                logger.info(
                    "[WorkerDaemon] Resuming suspended task %s (was suspended until %s)",
                    task_rec.task_id,
                    datetime.fromtimestamp(task_rec.next_retry_at or 0).isoformat(),
                )
                self.sm.resume_next(task_rec.task_id)
            self._active_tasks.add(task_rec.task_id)
            asyncio.create_task(self._process_task(task_rec.task_id))

    def _prune_old_tasks(self, now: float) -> None:
        """Clean up completed/failed tasks older than 1h from memory, not DB."""
        pass  # DB is the source of truth; memory pruning is optional

    # ── per-task processing ────────────────────────────────────────

    async def _process_task(self, task_id: str) -> None:
        """Run the full pipeline for a single task, handling 503 suspension."""
        task_start_time = time.time()
        retry_count = 0
        r2_frames_uploaded = 0
        
        try:
            logger.info("[WorkerDaemon] Processing task %s", task_id)
            pipeline = self._pipeline_factory(task_id)
            if pipeline is None:
                logger.error("[WorkerDaemon] No pipeline for task %s", task_id)
                self.sm.fail(task_id, "pipeline_factory returned None")
                self._active_tasks.discard(task_id)
                return

            # Inject shutdown event into pipeline if possible
            if hasattr(pipeline, 'shutdown_event'):
                pipeline.shutdown_event = self._shutdown_event
            
            await pipeline.run()

            # Pipeline finished — fire webhook if configured
            final_video = getattr(pipeline._state, "final_video_file", "")
            webhook_url = getattr(pipeline._state, "webhook_url", None) or self._webhook_base
            if webhook_url and final_video:
                await self._fire_webhook(webhook_url, task_id, final_video)

            self.sm.complete(task_id, final_video)
            task_duration = time.time() - task_start_time
            
            # Emit telemetry log
            logger.info(
                "[Telemetry] task_id=%s status=completed duration=%.1fs retries=%d r2_frames=%d",
                task_id, task_duration, retry_count, r2_frames_uploaded,
            )
            logger.info("[WorkerDaemon] Task %s COMPLETED in %.1fs", task_id, task_duration)

        except Exception as e:
            task_duration = time.time() - task_start_time
            logger.error("[WorkerDaemon] Task %s failed after %.1fs: %s", task_id, task_duration, e, exc_info=True)
            
            # Emit failure telemetry
            logger.info(
                "[Telemetry] task_id=%s status=failed duration=%.1fs retries=%d error=%s",
                task_id, task_duration, retry_count, str(e)[:100],
            )
            
            # Check if it's a 503-related failure
            tb = str(e)
            if "503" in tb or "server error" in tb.lower():
                retry_count += 1
                self.sm.suspend_for_503(task_id, delay_minutes=_SUSPEND_503_MINUTES)
            else:
                self.sm.fail(task_id, str(e)[:500])

        finally:
            self._active_tasks.discard(task_id)

    async def _fire_webhook(self, url: str, task_id: str, video_path: str) -> None:
        """POST webhook notification with task result."""
        import aiohttp
        payload = {
            "event": "video.completed",
            "task_id": task_id,
            "video_path": video_path,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, 
                    timeout=aiohttp.ClientTimeout(total=_WEBHOOK_TIMEOUT_SECONDS)
                ) as resp:
                    logger.info(
                        "[WorkerDaemon] Webhook fired -> %s (HTTP %s)", url, resp.status
                    )
                    if resp.status != 200:
                        logger.warning(
                            "[WorkerDaemon] Webhook returned non-200: %s", resp.status
                        )
        except Exception as e:
            logger.warning("[WorkerDaemon] Webhook POST failed: %s", e)
