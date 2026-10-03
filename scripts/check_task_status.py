import json, sys
d = json.load(sys.stdin)
print('status:', d.get('status'))
print('step:', d.get('current_step'))
print('paragraphs:', len(d.get('paragraphs', [])))
for p in d.get('paragraphs', []):
    print(f'  p{p["index"]}: text={p.get("text","")[:50]} prompt={p.get("scene_prompt","")[:60]}')
print('step_video_generation:', d.get('step_video_generation'))
print('step_concatenation:', d.get('step_concatenation'))
print('final_video_file:', repr(d.get('final_video_file','')))
print('error:', (d.get('error_traceback') or '')[:300])
