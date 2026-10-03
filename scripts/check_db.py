import sys
sys.path.insert(0, r'D:\agnes-video-generator')
from core.db.state_manager import get_state_manager
sm = get_state_manager()
for tid in ['cb620f990e04', '53065456eb27', '60105d5d438d']:
    t = sm.get_task(tid)
    if t:
        scenes = sm.list_scenes(tid)
        r2 = t.r2_cache_url[:40] if t.r2_cache_url else 'none'
        print(f'{tid}: status={t.status} scenes={t.current_scene}/{t.total_scenes} retry={t.next_retry_at} r2={r2}')
        for s in scenes:
            sr2 = s.r2_url[:40] if s.r2_url else 'none'
            print(f'  scene[{s.scene_index}]: {s.status} r2={sr2}')
    else:
        print(f'{tid}: NOT IN DB')
