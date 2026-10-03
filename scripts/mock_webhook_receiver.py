"""mock_webhook_receiver — Simple webhook receiver for testing daemon webhook delivery.

Run this script to verify that the WorkerDaemon can successfully POST webhook
notifications with the final video URL.

Usage:
    python scripts/mock_webhook_receiver.py

The server listens on port 8080 and logs all received webhook payloads.
It returns HTTP 200 OK for all requests to simulate a successful receiver.
"""
import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("MockWebhookReceiver")


class WebhookReceiver:
    """Simple HTTP server that receives and logs webhook payloads."""
    
    def __init__(self, port: int = 8080):
        self.port = port
        self.received_webhooks = []
        self._server = None
    
    async def handle_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle incoming HTTP request."""
        try:
            # Read request line
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                writer.close()
                return
            
            request_line = request_line.decode('utf-8').strip()
            logger.info("Received request: %s", request_line)
            
            # Read headers
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                line = line.decode('utf-8').strip()
                if not line:
                    break
                if ':' in line:
                    key, value = line.split(':', 1)
                    headers[key.strip().lower()] = value.strip()
            
            # Read body (if Content-Length present)
            body = b""
            content_length = int(headers.get('content-length', 0))
            if content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=10.0)
            
            # Parse JSON body
            payload = None
            if body:
                try:
                    payload = json.loads(body.decode('utf-8'))
                except json.JSONDecodeError as e:
                    logger.warning("Failed to parse JSON: %s", e)
            
            # Log received webhook
            if payload:
                logger.info("=" * 70)
                logger.info("WEBHOOK RECEIVED at %s", datetime.utcnow().isoformat() + "Z")
                logger.info("Event: %s", payload.get('event', 'unknown'))
                logger.info("Task ID: %s", payload.get('task_id', 'N/A'))
                logger.info("Video Path: %s", payload.get('video_path', 'N/A'))
                logger.info("Timestamp: %s", payload.get('timestamp', 'N/A'))
                logger.info("=" * 70)
                
                self.received_webhooks.append({
                    'received_at': datetime.utcnow().isoformat() + "Z",
                    'payload': payload,
                })
            
            # Send HTTP 200 OK response
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
            logger.error("Error handling request: %s", e)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
    
    async def start(self):
        """Start the webhook receiver server."""
        self._server = await asyncio.start_server(
            self.handle_request, '0.0.0.0', self.port
        )
        addr = self._server.sockets[0].getsockname()
        logger.info("Mock webhook receiver listening on http://%s:%d", addr[0], addr[1])
        logger.info("Waiting for webhook deliveries...")
        
        async with self._server:
            await self._server.serve_forever()
    
    def get_stats(self) -> dict:
        """Return statistics about received webhooks."""
        return {
            'total_received': len(self.received_webhooks),
            'webhooks': self.received_webhooks,
        }


async def main():
    parser = argparse.ArgumentParser(description="Mock webhook receiver for testing")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    args = parser.parse_args()
    
    receiver = WebhookReceiver(port=args.port)
    
    try:
        await receiver.start()
    except KeyboardInterrupt:
        logger.info("\nShutting down mock webhook receiver...")
        stats = receiver.get_stats()
        logger.info("Received %d webhook(s) during session", stats['total_received'])
        if stats['webhooks']:
            logger.info("Last webhook payload:")
            print(json.dumps(stats['webhooks'][-1]['payload'], indent=2, ensure_ascii=False))
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
