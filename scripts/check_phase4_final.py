import json, sys, urllib.request, time

sys.path.insert(0, r'D:\agnes-video-generator')
from core.db.state_manager import get_state_manager
sm = get_state_manager()

print('=' * 70)
print('Fase 4 — Resultado do Batch de 3 Manuscritos')
print('=' * 70)

for tid in ['cb620f990e04', '53065456eb27', '60105d5d438d']:
    resp = urllib.request.urlopen(f'http://localhost:8765/api/tasks/{tid}')
    d = json.loads(resp.read())
    logs_resp = urllib.request.urlopen(f'http://localhost:8765/api/tasks/{tid}/diagnostics')
    logs = json.loads(logs_resp.read())
    error_logs = logs.get('error_logs', [])
    recent_503s = [l for l in error_logs[-5:] if '503' in l.get('error_type', '')]

    print(f'\nTask {tid}:')
    print(f'  API status:   {d.get("status")}')
    print(f'  step:         {d.get("current_step")}')
    print(f'  paragraphs:   {len(d.get("paragraphs", []))}')
    print(f'  step_video:   {d.get("step_video_generation", "N/A")}')

    t = sm.get_task(tid)
    if t:
        print(f'  DB status:    {t.status}')
        print(f'  DB scenes:    {t.current_scene}/{t.total_scenes}')
        print(f'  DB retry_at:  {time.ctime(t.next_retry_at) if t.next_retry_at else "None"}')
        print(f'  DB error:     {t.error_detail or "none"}')
    else:
        print(f'  DB status:    NOT FOUND')

    if recent_503s:
        print(f'  503 logs:     {len(recent_503s)} hard 503(s) detected')

print()
print('=' * 70)
print('Todos os 3 tasks persistidos no SQLite')
print('=' * 70)
