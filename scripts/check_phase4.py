import json, sys, time, urllib.request

print("=" * 70)
print("Fase 4 — Batch de 3 manuscritos + Worker + State Machine")
print("=" * 70)

# Check all 3 tasks
for tid in ["f1f027b60af0", "6e1f4ef7d551", "00f8f6e51044"]:
    resp = urllib.request.urlopen(f"http://localhost:8765/api/tasks/{tid}")
    d = json.loads(resp.read())
    print(f"\nTask {tid}:")
    print(f"  status:       {d.get('status')}")
    print(f"  current_step: {d.get('current_step')}")
    print(f"  paragraphs:   {len(d.get('paragraphs', []))}")
    print(f"  step_video:   {d.get('step_video_generation', 'N/A')}")
    print(f"  final_video:  {repr(d.get('final_video_file', ''))}")
    tb = d.get('error_traceback') or ''
    if tb:
        print(f"  error:        {tb[:200]}")

# Check SQLite state DB
print("\n" + "=" * 70)
print("SQLite State DB:")
print("=" * 70)
import sys; sys.path.insert(0, r'D:\agnes-video-generator')
from core.db.state_manager import get_state_manager
sm = get_state_manager()
tasks = sm.list_tasks()
print(f"Total tasks in DB: {len(tasks)}")
for t in tasks:
    print(f"  {t.task_id}: status={t.status} scenes={t.current_scene}/{t.total_scenes}"
          f" retry_at={t.next_retry_at} error={t.error_detail or ''}")

# Check error logs for 503
print("\n" + "=" * 70)
print("Recent error logs (503s):")
print("=" * 70)
for tid in ["f1f027b60af0", "6e1f4ef7d551", "00f8f6e51044"]:
    resp = urllib.request.urlopen(f"http://localhost:8765/api/tasks/{tid}/diagnostics")
    d = json.loads(resp.read())
    logs = d.get('error_logs', [])
    for l in logs[-3:]:
        print(f"  {l['timestamp'][:19]} {l['error_type']}: {l['error_message'][:100]}")

print("\n" + "=" * 70)
print("Fase 4 validation COMPLETE")
print("=" * 70)
