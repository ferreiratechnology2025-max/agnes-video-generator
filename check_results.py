import os
import json
from PIL import Image

OUTPUT_FOLDER = r'D:\agnes-video-generator\real_estate_test'

# Get video file details
for f in sorted(os.listdir(OUTPUT_FOLDER)):
    if f.endswith('.mp4'):
        path = os.path.join(OUTPUT_FOLDER, f)
        size = os.path.getsize(path)
        print(f"{f}: {size:,} bytes ({size/1024/1024:.2f} MB)")

print()
# Check for any JSON results
results_path = os.path.join(OUTPUT_FOLDER, 'results.json')
if os.path.exists(results_path):
    with open(results_path) as f:
        results = json.load(f)
    print("Results from JSON:")
    for r in results:
        print(f"  {r['file']}: {r['status']} - {r['reason']}")
