from PIL import Image, ImageDraw
import os

os.chdir(r".\360Car\app\static")

for size in [192, 512]:
    img = Image.new('RGB', (size, size), '#0d1117')
    d = ImageDraw.Draw(img)
    # Draw "L360" text
    d.text((size//5, size//3), "Lotvue360", fill='#58a6ff')
    img.save(f'icon-{size}.png')
    print(f"Created icon-{size}.png")