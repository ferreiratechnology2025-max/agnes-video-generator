"""core.db.state_manager — SQLite-backed persistent state machine for the ViMax pipeline.

Each task is represented as a row.  Scenes are sub-rows.  The pipeline calls
``advance(task_id, scene_index, event)`` after every meaningful transition so
that if the process dies (SIGTERM, 503 loop, crash) the worker can resume
exactly where it left off.

Statuses:
    pending         – task created, not yet started
    building_scenes – ViMaxBrain is generating the storyboard
    scenes_ready    – storyboard complete, ready for video generation
    scene_N_t2v     – scene N submitted as T2V (no ref)
    scene_N_i2v     – scene N submitted as I2V (with R2 ref from scene N-1)
    scene_N_done    – scene N video downloaded, frame extracted, R2 uploaded
    scene_N_503     – scene N got a hard 503; suspended for retry
    r2_uploaded     – R2 frame for next scene is ready (only between scenes)
    suspending_503  – worker is backing off before retrying
    completed       – all scenes done, final concat finished
    failed          – unrecoverable error

Usage (singleton):
    sm = get_state_manager()
    sm.create_task("abc", "manuscript")
    sm.advance("abc", 0, "scene_0_t2v")
    sm.suspend_for_503("abc", delay_minutes=30)
    sm.complete("abc", "/path/to/final.mp4")
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    task_type     TEXT NOT NULL DEFAULT 'manuscript',
    status        TEXT NOT NULL DEFAULT 'pending',
    current_scene INTEGER NOT NULL DEFAULT 0,
    total_scenes  INTEGER NOT NULL DEFAULT 0,
    r2_cache_url  TEXT DEFAULT NULL,
    webhook_url   TEXT DEFAULT NULL,
    next_retry_at REAL   DEFAULT NULL,
    error_detail  TEXT DEFAULT NULL,
    created_at    REAL   NOT NULL,
    updated_at    REAL   NOT NULL
);

CREATE TABLE IF NOT EXISTS scenes (
    task_id           TEXT NOT NULL,
    scene_index       INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    video_id          TEXT DEFAULT NULL,
    video_path        TEXT DEFAULT NULL,
    r2_url            TEXT DEFAULT NULL,
    enhanced_prompt   TEXT DEFAULT NULL,
    narration_text    TEXT DEFAULT NULL,
    created_at        REAL   NOT NULL,
    updated_at        REAL   NOT NULL,
    PRIMARY KEY (task_id, scene_index),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_retry     ON tasks(next_retry_at)
    WHERE status IN ('suspended_503', 'pending');
CREATE INDEX IF NOT EXISTS idx_scenes_task     ON scenes(task_id);
CREATE INDEX IF NOT EXISTS idx_scenes_status   ON scenes(scene_index, status);
"""


@dataclass
class TaskRecord:
    task_id: str
    task_type: str
    status: str
    current_scene: int
    total_scenes: int
    r2_cache_url: Optional[str]
    webhook_url: Optional[str]
    next_retry_at: Optional[float]
    error_detail: Optional[str]
    created_at: float
    updated_at: float


@dataclass
class SceneRecord:
    task_id: str
    scene_index: int
    status: str
    video_id: Optional[str]
    video_path: Optional[str]
    r2_url: Optional[str]
    enhanced_prompt: Optional[str]
    narration_text: Optional[str]
    created_at: float
    updated_at: float


# ── Singleton ──────────────────────────────────────────────────────
_state_manager_instance: Optional["StateManager"] = None


def get_state_manager(db_path: str = None) -> "StateManager":
    """Get or create the global StateManager singleton."""
    global _state_manager_instance
    if _state_manager_instance is None:
        _state_manager_instance = StateManager(db_path)
        _state_manager_instance.init_db()
    return _state_manager_instance


def reset_state_manager() -> None:
    """Reset the singleton (for testing)."""
    global _state_manager_instance
    if _state_manager_instance:
        _state_manager_instance.close()
    _state_manager_instance = None


# ── StateManager ───────────────────────────────────────────────────
class StateManager:
    """Thread-safe SQLite state machine for ViMax pipeline tasks."""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                ".working_dir", "state.db",
            )
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def init_db(self) -> None:
        for stmt in _SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                self.conn.execute(stmt)
        self.conn.commit()
        logger.info("[StateManager] DB initialised: %s", self.db_path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── task CRUD ──────────────────────────────────────────────────

    def create_task(
        self, task_id: str, task_type: str = "manuscript", webhook_url: str = None,
    ) -> TaskRecord:
        now = time.time()
        self.conn.execute(
            """INSERT OR REPLACE INTO tasks
               (task_id, task_type, status, current_scene, total_scenes,
                r2_cache_url, webhook_url, next_retry_at, error_detail,
                created_at, updated_at)
               VALUES (?, ?, 'pending', 0, 0, NULL, ?, NULL, NULL, ?, ?)""",
            (task_id, task_type, webhook_url, now, now),
        )
        self.conn.commit()
        logger.info("[StateManager] Task created: %s", task_id)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return TaskRecord(**dict(row)) if row else None

    def list_tasks(
        self,
        statuses: List[str] = None,
        max_next_retry: float = None,
        limit: int = 100,
    ) -> List[TaskRecord]:
        conditions, params = [], []
        if statuses:
            ph = ",".join("?" * len(statuses))
            conditions.append(f"status IN ({ph})")
            params.extend(statuses)
        if max_next_retry is not None:
            conditions.append("next_retry_at <= ?")
            params.append(max_next_retry)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self.conn.execute(
            f"SELECT * FROM tasks {where} ORDER BY created_at LIMIT ?",
            params + [limit],
        ).fetchall()
        return [TaskRecord(**dict(r)) for r in rows]

    # ── scene CRUD ─────────────────────────────────────────────────

    def create_scene(
        self, task_id: str, scene_index: int,
        enhanced_prompt: str = "", narration_text: str = "",
    ) -> SceneRecord:
        now = time.time()
        self.conn.execute(
            """INSERT OR REPLACE INTO scenes
               (task_id, scene_index, status, enhanced_prompt, narration_text,
                created_at, updated_at)
               VALUES (?, ?, 'pending', ?, ?, ?, ?)""",
            (task_id, scene_index, enhanced_prompt, narration_text, now, now),
        )
        self.conn.execute(
            "UPDATE tasks SET total_scenes = MAX(total_scenes, ?) WHERE task_id = ?",
            (scene_index + 1, task_id),
        )
        self.conn.commit()
        return self.get_scene(task_id, scene_index)

    def get_scene(self, task_id: str, scene_index: int) -> Optional[SceneRecord]:
        row = self.conn.execute(
            "SELECT * FROM scenes WHERE task_id = ? AND scene_index = ?",
            (task_id, scene_index),
        ).fetchone()
        return SceneRecord(**dict(row)) if row else None

    def list_scenes(self, task_id: str) -> List[SceneRecord]:
        rows = self.conn.execute(
            "SELECT * FROM scenes WHERE task_id = ? ORDER BY scene_index", (task_id,)
        ).fetchall()
        return [SceneRecord(**dict(r)) for r in rows]

    # ── transitions ────────────────────────────────────────────────

    def advance(self, task_id: str, scene_index: int, event: str) -> None:
        now = time.time()
        self.conn.execute(
            "UPDATE scenes SET status = ?, updated_at = ? "
            "WHERE task_id = ? AND scene_index = ?",
            (event, now, task_id, scene_index),
        )
        self.conn.execute(
            "UPDATE tasks SET current_scene = ?, updated_at = ? WHERE task_id = ?",
            (scene_index, now, task_id),
        )
        self.conn.commit()
        logger.info("[StateManager] %s scene[%d] = %s", task_id, scene_index, event)

    def suspend_for_503(self, task_id: str, delay_minutes: int = 30) -> None:
        next_retry = time.time() + delay_minutes * 60
        now = time.time()
        self.conn.execute(
            """UPDATE tasks SET status='suspended_503', next_retry_at=?,
               error_detail='HTTP 503 from Agnes Video API', updated_at=?
               WHERE task_id=?""",
            (next_retry, now, task_id),
        )
        self.conn.commit()
        logger.warning(
            "[StateManager] %s SUSPENDED for 503, retry at %s",
            task_id, datetime.fromtimestamp(next_retry).isoformat(),
        )

    def resume_next(self, task_id: str) -> None:
        now = time.time()
        self.conn.execute(
            "UPDATE tasks SET status='pending', next_retry_at=NULL, updated_at=? WHERE task_id=?",
            (now, task_id),
        )
        self.conn.commit()

    def complete(self, task_id: str, final_video_path: str = "") -> None:
        now = time.time()
        self.conn.execute(
            "UPDATE tasks SET status='completed', r2_cache_url=?, updated_at=? WHERE task_id=?",
            (final_video_path, now, task_id),
        )
        self.conn.commit()
        logger.info("[StateManager] Task %s COMPLETED", task_id)

    def fail(self, task_id: str, reason: str) -> None:
        now = time.time()
        self.conn.execute(
            "UPDATE tasks SET status='failed', error_detail=?, updated_at=? WHERE task_id=?",
            (reason, now, task_id),
        )
        self.conn.commit()
        logger.error("[StateManager] Task %s FAILED: %s", task_id, reason)

    # ── worker helpers ─────────────────────────────────────────────

    def get_pending_or_suspended(self, now: float = None) -> List[TaskRecord]:
        if now is None:
            now = time.time()
        rows = self.conn.execute(
            """SELECT * FROM tasks
               WHERE status IN ('pending', 'suspended_503')
                 AND (next_retry_at IS NULL OR next_retry_at <= ?)
               ORDER BY
                 CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                 created_at
               LIMIT 50""",
            (now,),
        ).fetchall()
        return [TaskRecord(**dict(r)) for r in rows]

    def get_scene_status(self, task_id: str, scene_index: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT status FROM scenes WHERE task_id = ? AND scene_index = ?",
            (task_id, scene_index),
        ).fetchone()
        return row["status"] if row else None

    def get_all_scene_statuses(self, task_id: str) -> Dict[int, str]:
        rows = self.conn.execute(
            "SELECT scene_index, status FROM scenes WHERE task_id = ? ORDER BY scene_index",
            (task_id,),
        ).fetchall()
        return {r["scene_index"]: r["status"] for r in rows}
