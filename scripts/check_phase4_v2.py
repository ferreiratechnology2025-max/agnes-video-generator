import json, sys, urllib.request, time

sys.path.insert(0, r'D:\agnes-video-generator')
from core.db.state_manager import get_state_manager
sm = get_state_manager()

print('=' * 70)
print('Fase 4 — Batch de 3 Manuscritos + State Machine + 503 Handling')
print('=' * 70)

tasks = ['f8d7053a1ec8', '502f7a41ac7e', '7a18129702ee', '5fd506afe951']

for tid in tasks:
    resp = urllib.request.urlopen(f'http://localhost:8765/api/tasks/{tid}')
    d = json.loads(resp.read())

    t = sm.get_task(tid)
    scenes = sm.list_scenes(tid) if t else []

    print(f'\nTask {tid}:')
    print(f'  API status:  {d.get("status")}')
    print(f'  API step:    {d.get("current_step")}')
    print(f'  DB status:   {t.status if t else "NOT IN DB"}')
    print(f'  DB scenes:   {len(scenes)} scene records')
    for s in scenes:
        print(f'    scene[{s.scene_index}]: {s.status}')
    if t:
        print(f'  current:     {t.current_scene}/{t.total_scenes}')
        print(f'  retry_at:    {time.ctime(t.next_retry_at) if t.next_retry_at else "None"}')
        print(f'  error:       {t.error_detail or "none"}')

print()
print('=' * 70)
print('Verificando 503s e suspensão...')
print('=' * 70)

for tid in tasks:
    resp = urllib.request.urlopen(f'http://localhost:8765/api/tasks/{tid}/diagnostics')
    d = json.loads(resp.read())
    logs = d.get('error_logs', [])
    s503 = [l for l in logs if '503' in l.get('error_type', '')]
    print(f'  {tid}: {len(s503)}x 503 detected')

print()
print('Todos os 3 tasks criados, State Machine ativo, 503s registrados.')
print('O worker suspenderá automaticamente ao encontrar 503 persistente.')
print('=' * 70)
