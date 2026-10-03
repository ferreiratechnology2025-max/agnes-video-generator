"""
Agnes Real-Estate Fidelity Test
Generates i2v clips from property photos using Agnes v2.0
"""
import requests
import time
import json
import os
import glob
import shutil

# Configuration
AGNES_URL = "http://localhost:8765"
BASE = r'C:\Users\ferre\Desktop\Marilene'
# Find the correct folder (handles the "Itália" special character)
_source_folders = glob.glob(os.path.join(BASE, 'Jardins*01'))
SOURCE_FOLDER = _source_folders[0] if _source_folders else os.path.join(BASE, 'Jardins Italia 01')
print(f"Source folder: {SOURCE_FOLDER}")
OUTPUT_FOLDER = r'D:\agnes-video-generator\real_estate_test'

# Photo mapping (file -> category)
PHOTOS = [
    {"file": "01.jpeg", "category": "fachada"},
    {"file": "03.jpeg", "category": "sala"},
    {"file": "05.jpeg", "category": "cozinha"},
    {"file": "11.jpeg", "category": "suite"},
    {"file": "20.jpeg", "category": "piscina"},
    {"file": "04.jpeg", "category": "paisagismo"},
]

# Prompt and negative prompt
PROMPT = "Create a high-end vertical real-estate video clip from this property photo with subtle cinematic camera motion. Preserve the exact architecture, room layout, materials, furniture, pool, landscaping, lighting, proportions, and details from the source image. Do not redesign or reinterpret the property. Keep the scene realistic, stable, elegant, and faithful to the original photo."

NEGATIVE_PROMPT = "architecture changes, altered room layout, extra windows, missing windows, changed pool shape, distorted furniture, warped walls, morphing objects, extra objects, invented spaces, flicker, unstable details, unrealistic motion, blur, duplication, people, text, watermark"

def create_task(photo_file, category, try_num=1):
    """Create an i2v task for a photo."""
    source_path = os.path.join(SOURCE_FOLDER, photo_file)
    if not os.path.exists(source_path):
        print(f"ERROR: Source photo not found: {source_path}")
        return None
    
    print(f"Submitting: {category} ({photo_file}) try {try_num}")
    
    with open(source_path, 'rb') as f:
        files = {'reference_image': (photo_file, f, 'image/jpeg')}
        data = {
            'prompt': PROMPT,
            'mode': 'i2v',
            'duration': 5,
            'video_width': 768,
            'video_height': 1152,
            'negative_prompt': NEGATIVE_PROMPT,
            'system_prompt': '',
        }
        
        response = requests.post(f"{AGNES_URL}/api/tasks/simple", files=files, data=data)
    
    if response.status_code == 200:
        result = response.json()
        if result.get('ok'):
            task_id = result['task_id']
            print(f"  Task created: {task_id}")
            return task_id
        else:
            print(f"  ERROR: {result}")
            return None
    else:
        print(f"  ERROR: HTTP {response.status_code}")
        return None

def wait_for_task(task_id, timeout=300):
    """Wait for a task to complete."""
    start = time.time()
    while time.time() - start < timeout:
        response = requests.get(f"{AGNES_URL}/api/tasks/{task_id}")
        if response.status_code == 200:
            data = response.json()
            status = data.get('status')
            progress = data.get('current_progress', 0)
            print(f"  Status: {status}, Progress: {progress:.0%}")
            
            if status == 'completed':
                return data.get('final_video_file')
            elif status == 'failed':
                print(f"  FAILED: {data.get('error_traceback', 'Unknown error')}")
                return None
        
        time.sleep(10)
    
    print(f"  TIMEOUT after {timeout}s")
    return None

def download_video(video_path, output_path):
    """Download video from server."""
    # The video is served via the working directory
    # We need to copy it from the working directory
    if os.path.exists(video_path):
        shutil.copy2(video_path, output_path)
        print(f"  Saved: {output_path}")
        return True
    else:
        print(f"  ERROR: Video file not found: {video_path}")
        return False

def main():
    # Create output directory
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    
    # Check server
    try:
        response = requests.get(f"{AGNES_URL}/api/config")
        if response.status_code != 200:
            print("ERROR: Server not responding")
            return
        print("Server is running")
    except:
        print("ERROR: Cannot connect to server")
        return
    
    results = []
    
    for i, photo in enumerate(PHOTOS):
        print(f"\n{'='*60}")
        print(f"[{i+1}/6] Processing: {photo['category']} ({photo['file']})")
        print(f"{'='*60}")
        
        # Create task
        task_id = create_task(photo['file'], photo['category'])
        if not task_id:
            results.append({
                'file': f"{i+1:02d}_{photo['category']}_i2v_v20_try1.mp4",
                'source': photo['file'],
                'category': photo['category'],
                'duration': 5,
                'seed': None,
                'status': 'FAILED',
                'reason': 'Task creation failed'
            })
            continue
        
        # Wait for completion
        video_path = wait_for_task(task_id)
        if not video_path:
            results.append({
                'file': f"{i+1:02d}_{photo['category']}_i2v_v20_try1.mp4",
                'source': photo['file'],
                'category': photo['category'],
                'duration': 5,
                'seed': None,
                'status': 'FAILED',
                'reason': 'Video generation failed or timed out'
            })
            continue
        
        # Download video
        output_file = f"{i+1:02d}_{photo['category']}_i2v_v20_try1.mp4"
        output_path = os.path.join(OUTPUT_FOLDER, output_file)
        
        if download_video(video_path, output_path):
            results.append({
                'file': output_file,
                'source': photo['file'],
                'category': photo['category'],
                'duration': 5,
                'seed': None,
                'status': 'GENERATED',
                'reason': 'Video generated successfully'
            })
        else:
            results.append({
                'file': output_file,
                'source': photo['file'],
                'category': photo['category'],
                'duration': 5,
                'seed': None,
                'status': 'FAILED',
                'reason': 'Download failed'
            })
    
    # Save results
    results_path = os.path.join(OUTPUT_FOLDER, 'results.json')
    with open(results_path, 'w') as f:
        json.dump(results, f, indent=2)
    
    print(f"\n{'='*60}")
    print("RESULTS SUMMARY")
    print(f"{'='*60}")
    for r in results:
        print(f"{r['file']}: {r['status']} - {r['reason']}")
    
    print(f"\nResults saved to: {results_path}")

if __name__ == "__main__":
    main()
