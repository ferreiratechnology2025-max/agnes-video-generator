import sys, json, urllib.request
sys.path.insert(0, r'D:\agnes-video-generator')
from core.db.state_manager import get_state_manager
sm = get_state_manager()
t = sm.get_task('ec2532861697')
if t:
    print(f'status={t.status} retry={t.next_retry_at} error={t.error_detail}')
    scenes = sm.list_scenes('ec2532861697')
    for s in scenes:
        print(f'  scene[{s.scene_index}]: {s.status}')
else:
    print('NOT IN DB')
resp = urllib.request.urlopen('http://localhost:8765/api/tasks/ec2532861697')
d = json.loads(resp.read())
sv = d.get('step_video_generation', 'N/A')
st = d.get('status', 'N/A')
print(f'API: step_video={sv} status={st}')
