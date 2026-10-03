import json, sys
d = json.load(open(r'C:\temp\task_f3.json', 'r', encoding='utf-8'))
print('step:', d.get('step_video_generation'), 'status:', d.get('status'))
for p in d.get('paragraphs', []):
    print(f'  p{p["index"]}: vf={repr(p.get("video_file",""))[:60]} vid={p.get("video_id","")[:16]}')
print('final:', repr(d.get('final_video_file', '')))
print('error:', (d.get('error_traceback') or '')[:300])
