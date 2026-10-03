"""Phase 5 Integration Test — End-to-End Daemon + Webhook Validation.

This script starts:
1. A mock webhook receiver on port 8080
2. The WorkerDaemon with a real SQLite state manager
3. Creates test tasks and verifies telemetry + webhook delivery

Usage:
    python scripts/phase5_integration_test.py
    
Requires:
    - No other service on port 8080
    - SQLite (built-in)
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

logger = logging.getLogger("Phase5Integration")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logger.addHandler(handler)


class MockWebhookServer:
    """Simple asyncio HTTP server for receiving webhooks."""
    
    def __init__(self, port=8080):
        self.port = port
        self.webhooks = []
        self._server = None
    
    async def handle_request(self, reader, writer):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                writer.close()
                return
            
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                line = line.decode('utf-8').strip()
                if not line:
                    break
                if ':' in line:
                    key, value = line.split(':', 1)
                    headers[key.strip().lower()] = value.strip()
            
            body = b""
            content_length = int(headers.get('content-length', 0))
            if content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=10.0)
            
            payload = None
            if body:
                try:
                    payload = json.loads(body.decode('utf-8'))
                except json.JSONDecodeError:
                    pass
            
            if payload:
                self.webhooks.append({
                    'received_at': datetime.utcnow().isoformat() + "Z",
                    'payload': payload,
                })
                logger.info(
                    "[Webhook] Received: event=%s task_id=%s video_path=%s",
                    payload.get('event'),
                    payload.get('task_id'),
                    str(payload.get('video_path', ''))[:50],
                )
            
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 2\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"OK"
            )
            writer.write(response)
            await writer.drain()
        except Exception as e:
            logger.error("[Webhook] Error: %s", e)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
    
    async def start(self):
        self._server = await asyncio.start_server(self.handle_request, '0.0.0.0', self.port)
        addr = self._server.sockets[0].getsockname()
        logger.info("[Webhook] Server listening on http://%s:%d", addr[0], addr[1])
        async with self._server:
            await self._server.serve_forever()
    
    def get_stats(self):
        return {
            'total_received': len(self.webhooks),
            'webhooks': self.webhooks,
        }


async def run_integration_test():
    """Run the full Phase 5 integration test."""
    from core.db.state_manager import get_state_manager, reset_state_manager
    from core.worker.daemon import WorkerDaemon
    
    # Reset state manager for clean test
    reset_state_manager()
    db_path = os.path.join(tempfile.mkdtemp(), "state.db")
    sm = get_state_manager(db_path)
    sm.init_db()
    logger.info("[Test] SQLite DB initialized: %s", db_path)
    
    # Create mock pipeline factory
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
                logger.info("[MockPipeline] Task %s stopping...", self.task_id)
            
            async def run(self):
                logger.info("[MockPipeline] Running task %s...", self.task_id)
                await asyncio.sleep(0.3)
                logger.info("[MockPipeline] Task %s completed", self.task_id)
        
        return MockPipeline()
    
    # Start webhook server
    webhook_server = MockWebhookServer(port=8080)
    webhook_task = asyncio.create_task(webhook_server.start())
    await asyncio.sleep(0.5)
    
    # Start daemon
    daemon = WorkerDaemon(sm, pipeline_factory, webhook_base_url="http://localhost:8080/webhook")
    daemon.start()
    logger.info("[Test] Daemon started")
    
    # Create test tasks
    logger.info("[Test] Creating test tasks...")
    sm.create_task("phase5-test-001", webhook_url="http://localhost:8080/webhook")
    sm.create_task("phase5-test-002", webhook_url="http://localhost:8080/webhook")
    sm.create_task("phase5-test-003", webhook_url="http://localhost:8080/webhook")
    
    # Wait for tasks to complete
    logger.info("[Test] Waiting for tasks to complete...")
    await asyncio.sleep(3)
    
    # Check results
    logger.info("")
    logger.info("=" * 70)
    logger.info("RESULTS")
    logger.info("=" * 70)
    
    # Check task statuses
    for i in range(1, 4):
        task_id = f"phase5-test-00{i}"
        task = sm.get_task(task_id)
        logger.info("[Result] %s: status=%s", task_id, task.status if task else "NOT FOUND")
    
    # Check webhooks
    stats = webhook_server.get_stats()
    logger.info("[Result] Webhooks received: %d", stats['total_received'])
    
    if stats['webhooks']:
        logger.info("[Result] Last webhook payload:")
        print(json.dumps(stats['webhooks'][-1]['payload'], indent=2, ensure_ascii=False))
    
    # Trigger graceful shutdown
    logger.info("")
    logger.info("[Test] Triggering graceful shutdown...")
    daemon.stop()
    await asyncio.sleep(2)
    
    # Cleanup
    webhook_task.cancel()
    try:
        await webhook_task
    except asyncio.CancelledError:
        pass
    
    sm.close()
    
    logger.info("")
    logger.info("=" * 70)
    logger.info("Phase 5 Integration Test COMPLETE")
    logger.info("=" * 70)
    
    return stats['total_received'] == 3


async def main():
    parser = argparse.ArgumentParser(description="Phase 5 Integration Test")
    parser.add_argument("--port", type=int, default=8080, help="Webhook receiver port")
    args = parser.parse_args()
    
    logger.info("Phase 5 Integration Test Starting...")
    logger.info("Timestamp: %s", datetime.utcnow().isoformat() + "Z")
    logger.info("")
    
    try:
        success = await run_integration_test()
        sys.exit(0 if success else 1)
    except Exception as e:
        logger.error("[Test] Failed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
