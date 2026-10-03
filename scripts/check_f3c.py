import json
d = json.load(open(r'C:\temp\task_f3c.json', 'r', encoding='utf-8'))
print('step:', d.get('step_video_generation'))
for p in d.get('paragraphs', []):
    print(f'p{p["index"]}: vid={p.get("video_id","")[:16]} vf={repr(p.get("video_file",""))[:60]}')
print('final:', repr(d.get('final_video_file', '')))
print('error:', (d.get('error_traceback') or '')[:300])
