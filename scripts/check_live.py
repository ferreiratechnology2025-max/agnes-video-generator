import json, sys
# Read from the file directly
import urllib.request
try:
    resp = urllib.request.urlopen('http://localhost:8765/api/tasks/c33fa2c3e5f4')
    d = json.loads(resp.read())
except Exception as e:
    print('fetch error:', e)
    sys.exit(1)

print('status:', d.get('status'))
print('step:', d.get('current_step'))
print('step_video_generation:', d.get('step_video_generation'))
print('paragraphs:', len(d.get('paragraphs', [])))
for p in d.get('paragraphs', []):
    print(f'  p{p["index"]}: vid={p.get("video_id","")[:16]} vf={repr(p.get("video_file",""))[:60]}')
print('final_video_file:', repr(d.get('final_video_file','')))
print('error:', (d.get('error_traceback') or '')[:400])
