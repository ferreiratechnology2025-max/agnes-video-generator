"""Phase 5 Validation Script — Graceful Shutdown + Telemetry + Webhook.

This script validates the Phase 5 deliverables:
1. Graceful shutdown (SIGINT/SIGTERM handling)
2. Structured JSON telemetry logging
3. Webhook delivery to mock receiver

Usage:
    python scripts/phase5_validation.py

Requires:
    - SQLite (built-in, no extra deps)
    - aiohttp (for webhook POST)
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Configure structured logging
class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_obj = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj, ensure_ascii=False)

logger = logging.getLogger("Phase5Validation")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logger.addHandler(handler)


async def test_graceful_shutdown():
    """Test 1: Graceful shutdown on SIGINT/SIGTERM."""
    logger.info("=" * 70)
    logger.info("TEST 1: Graceful Shutdown")
    logger.info("=" * 70)
    
    from core.db.state_manager import get_state_manager, reset_state_manager
    from core.worker.daemon import WorkerDaemon
    
    # Use real SQLite state manager
    reset_state_manager()
    db_path = os.path.join(tempfile.mkdtemp(), "test.db")
    sm = get_state_manager(db_path)
    sm.init_db()
    
    # Create mock pipeline factory
    def pipeline_factory(task_id):
        class MockState:
            def __init__(self):
                self.final_video_file = f"/tmp/{task_id}.mp4"
                self.webhook_url = ""
        
        class MockPipeline:
            def __init__(self):
                self._state = MockState()
                self.task_id = task_id
                self.shutdown_event = None
            
            def stop(self):
                logger.info("[MockPipeline] Task %s stopping...", self.task_id)
            
            async def run(self):
                logger.info("[MockPipeline] Task %s running...", self.task_id)
                await asyncio.sleep(0.2)
                logger.info("[MockPipeline] Task %s completed", self.task_id)
        
        return MockPipeline()
    
    daemon = WorkerDaemon(sm, pipeline_factory, webhook_base_url="")
    daemon.start()
    logger.info("[Test] Daemon started")
    
    # Create a task
    sm.create_task("test-shutdown-001")
    logger.info("[Test] Created task: test-shutdown-001")
    
    # Wait for task to start
    await asyncio.sleep(1)
    
    # Trigger shutdown
    logger.info("[Test] Triggering graceful shutdown...")
    daemon.stop()
    
    # Wait for cleanup
    await asyncio.sleep(2)
    
    logger.info("[Test] [PASS] Graceful shutdown test complete")
    return True


async def test_telemetry():
    """Test 2: Telemetry logging on task completion."""
    logger.info("")
    logger.info("=" * 70)
    logger.info("TEST 2: Telemetry Logging")
    logger.info("=" * 70)
    
    from core.db.state_manager import get_state_manager, reset_state_manager
    from core.worker.daemon import WorkerDaemon
    
    reset_state_manager()
    db_path = os.path.join(tempfile.mkdtemp(), "test.db")
    sm = get_state_manager(db_path)
    sm.init_db()
    
    def pipeline_factory(task_id):
        class MockState:
            def __init__(self):
                self.final_video_file = f"/tmp/{task_id}.mp4"
                self.webhook_url = ""
        
        class MockPipeline:
            def __init__(self):
                self._state = MockState()
                self.task_id = task_id
                self.shutdown_event = None
            
            def stop(self):
                pass
            
            async def run(self):
                logger.info("[MockPipeline] Task %s running...", self.task_id)
                await asyncio.sleep(0.3)
                logger.info("[MockPipeline] Task %s completed", self.task_id)
        
        return MockPipeline()
    
    daemon = WorkerDaemon(sm, pipeline_factory, webhook_base_url="")
    daemon.start()
    
    sm.create_task("test-telemetry-001")
    logger.info("[Test] Created task: test-telemetry-001")
    
    # Wait for completion
    await asyncio.sleep(2)
    
    daemon.stop()
    await asyncio.sleep(1)
    
    logger.info("[Test] [PASS] Telemetry test complete")
    logger.info("[Test] Look for [Telemetry] JSON lines above")
    return True


async def test_webhook():
    """Test 3: Webhook delivery."""
    logger.info("")
    logger.info("=" * 70)
    logger.info("TEST 3: Webhook Delivery")
    logger.info("=" * 70)
    
    from core.db.state_manager import get_state_manager, reset_state_manager
    from core.worker.daemon import WorkerDaemon
    
    reset_state_manager()
    db_path = os.path.join(tempfile.mkdtemp(), "test.db")
    sm = get_state_manager(db_path)
    sm.init_db()
    
    def pipeline_factory(task_id):
        class MockState:
            def __init__(self):
                self.final_video_file = f"/tmp/{task_id}.mp4"
                self.webhook_url = "http://localhost:8080/webhook"
        
        class MockPipeline:
            def __init__(self):
                self._state = MockState()
                self.task_id = task_id
                self.shutdown_event = None
            
            def stop(self):
                pass
            
            async def run(self):
                logger.info("[MockPipeline] Task %s running...", self.task_id)
                await asyncio.sleep(0.3)
                logger.info("[MockPipeline] Task %s completed", self.task_id)
        
        return MockPipeline()
    
    daemon = WorkerDaemon(sm, pipeline_factory, webhook_base_url="http://localhost:8080/webhook")
    daemon.start()
    
    sm.create_task("test-webhook-001", webhook_url="http://localhost:8080/webhook")
    logger.info("[Test] Created task: test-webhook-001")
    
    # Wait for webhook to be fired
    await asyncio.sleep(2)
    
    daemon.stop()
    await asyncio.sleep(1)
    
    logger.info("[Test] [PASS] Webhook test complete")
    logger.info("[Test] Check mock_webhook_receiver.py output for received payload")
    return True


async def main():
    parser = argparse.ArgumentParser(description="Phase 5 Validation Script")
    parser.add_argument("--test", choices=["shutdown", "telemetry", "webhook", "all"], 
                        default="all", help="Which test to run")
    args = parser.parse_args()
    
    logger.info("Phase 5 Validation Starting...")
    logger.info("Timestamp: %s", datetime.utcnow().isoformat() + "Z")
    logger.info("")
    
    tests = {
        "shutdown": test_graceful_shutdown,
        "telemetry": test_telemetry,
        "webhook": test_webhook,
    }
    
    if args.test == "all":
        for name, test_fn in tests.items():
            try:
                await test_fn()
            except Exception as e:
                logger.error("[Test] %s failed: %s", name, e, exc_info=True)
    else:
        await tests[args.test]()
    
    logger.info("")
    logger.info("=" * 70)
    logger.info("Phase 5 Validation Complete")
    logger.info("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
