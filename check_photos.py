from PIL import Image
import os
import glob
import json

base = r'C:\Users\ferre\Desktop\Marilene'
folders = glob.glob(os.path.join(base, 'Jardins*01'))
folder = folders[0]

# Try to get EXIF data or any description from photos
for f in sorted(os.listdir(folder)):
    if f.lower().endswith(('.jpeg', '.jpg')) and f.startswith(('01','02','03','04','05','06','07','08','09','10','11','12','13','14','15','16','17','18','19','20')):
        path = os.path.join(folder, f)
        img = Image.open(path)
        w, h = img.size
        orient = "portrait" if h > w else ("landscape" if w > h else "square")
        info = img.info
        exif_data = {}
        if hasattr(img, '_getexif') and img._getexif():
            exif = img._getexif()
            # Common EXIF tags
            tag_names = {270: 'ImageDescription', 271: 'Make', 272: 'Model', 305: 'Software', 306: 'DateTime', 33432: 'Copyright'}
            for tag_id, value in exif.items():
                if tag_id in tag_names:
                    exif_data[tag_names[tag_id]] = str(value)[:100]
        print(f"{f}: {w}x{h} ({orient}) exif={exif_data if exif_data else 'none'}")
