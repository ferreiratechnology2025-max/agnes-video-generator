import json
d = json.load(open(r'D:\temp\task.json', 'r', encoding='utf-8'))
print('step:', d.get('step_video_generation'))
print('status:', d.get('status'))
for p in d.get('paragraphs', []):
    print(f'  para {p["index"]}: video={repr(p.get("video_file",""))[:80]}')
print('final_video_file:', repr(d.get('final_video_file', '')))
print('error:', (d.get('error_traceback') or '')[:300])
