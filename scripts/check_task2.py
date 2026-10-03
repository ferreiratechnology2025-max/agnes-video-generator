import json, sys
d = json.load(open(r'D:\temp\task_f2b5e02fd364.json', 'r', encoding='utf-8'))
print('step:', d.get('step_video_generation'))
print('status:', d.get('status'))
for p in d.get('paragraphs', []):
    print(f'  p{p["index"]}: video_file={repr(p.get("video_file",""))[:80]} video_id={p.get("video_id","")[:16]}')
print('final_video_file:', repr(d.get('final_video_file', '')))
print('error:', (d.get('error_traceback') or '')[:300])
