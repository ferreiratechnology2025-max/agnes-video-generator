"""Verify generated clips metadata using imageio-ffmpeg binary."""
import os
import subprocess
import imageio_ffmpeg

OUTPUT_FOLDER = r'D:\agnes-video-generator\real_estate_test'

# Get the bundled ffmpeg binary
ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
print(f"ffmpeg: {ffmpeg}")

for f in sorted(os.listdir(OUTPUT_FOLDER)):
    if f.endswith('.mp4'):
        path = os.path.join(OUTPUT_FOLDER, f)
        cmd = [ffmpeg, '-i', path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        # ffmpeg writes info to stderr
        info = result.stderr
        duration = "?"
        resolution = "?"
        fps = "?"
        for line in info.split('\n'):
            if 'Duration' in line and 'start' in line:
                duration = line.split('Duration:')[1].split(',')[0].strip()
            if 'Video:' in line:
                # Extract resolution and fps
                res = None
                for token in line.split(','):
                    token = token.strip()
                    if 'x' in token and token.replace('x', '').isdigit():
                        res = token
                    if 'fps' in token or 'fps,' in token:
                        fps = token.split('fps')[0].strip()
                resolution = res or "?"
        size_mb = os.path.getsize(path) / 1024 / 1024
        print(f"{f}: {size_mb:.2f} MB, duration={duration}, res={resolution}, fps={fps}")
