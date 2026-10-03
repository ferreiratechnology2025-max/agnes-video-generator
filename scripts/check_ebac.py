import json, sys, urllib.request, time

try:
    resp = urllib.request.urlopen('http://localhost:8765/api/tasks/ebac75664859')
    d = json.loads(resp.read())
except Exception as e:
    print('fetch error:', e)
    sys.exit(1)

print(f"status: {d.get('status')}")
print(f"step: {d.get('current_step')}")
print(f"step_video_generation: {d.get('step_video_generation')}")
print(f"paragraphs: {len(d.get('paragraphs', []))}")
for p in d.get('paragraphs', []):
    print(f"  p{p['index']}: vid={repr(p.get('video_id',''))[:16]} vf={repr(p.get('video_file',''))[:60]}")
print(f"final_video_file: {repr(d.get('final_video_file',''))}")
tb = d.get('error_traceback') or ''
if tb:
    print(f"error:\n{tb[:500]}")
