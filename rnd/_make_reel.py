"""Assemble LOTVUE_SPYNE_CLONE_DEMO.mp4: title card + per-car label cards +
the four studio spins, all normalized to 1280x720@24. Numbers on the cards
come from each car's studio_spin/meta.json — measured, not typed."""
import json, subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

BASE = Path(r'E:\ABU\01_PRODUCTS\360Car-LotVue')
ST = BASE / 'app' / 'storage'
OUT = BASE / 'rnd' / '_reel'
OUT.mkdir(exist_ok=True)

CARS = [
    ('5LMJJ2KT0LEL00884', 'showroom_floor', 'Lincoln Navigator', 'Showroom floor'),
    ('1HGCV1F31MA037775', 'studio_white', 'Honda Accord Sport', 'White studio'),
    ('4S4BSENC1K3205956', 'studio_dark', 'Subaru Outback', 'Dark studio'),
]

def font(sz):
    for f in (r'C:\Windows\Fonts\segoeuib.ttf', r'C:\Windows\Fonts\arialbd.ttf'):
        if Path(f).exists():
            return ImageFont.truetype(f, sz)
    return ImageFont.load_default()

def card(path, lines):
    im = Image.new('RGB', (1280, 720), (10, 11, 14))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 700, 1280, 720], fill=(48, 108, 255))
    y = 250
    for txt, sz, col in lines:
        f = font(sz)
        w = d.textlength(txt, font=f)
        d.text(((1280 - w) / 2, y), txt, font=f, fill=col)
        y += sz + 26
    im.save(path)

card(OUT / 'card_main.png', [
    ('LOTVUE PRO', 64, (255, 255, 255)),
    ('Studio 360 spin from the photos a dealer already has', 34, (170, 190, 230)),
    ('No reshoot · No turntable · AI-selected, AI-ordered, AI-staged', 26, (120, 140, 180)),
])

inputs, filters, idx = [], [], 0
inputs += ['-loop', '1', '-t', '2.2', '-i', str(OUT / 'card_main.png')]
filters.append(f'[{idx}:v]fps=24,scale=1280:720,setsar=1[v{idx}]'); idx += 1

for vin, bg, name, bgname in CARS:
    meta = json.loads((ST / vin / 'studio_spin' / 'meta.json').read_text())
    n_gal, n_kept = meta['gallery_frames'], meta['kept_exterior']
    cpath = OUT / f'card_{vin}.png'
    card(cpath, [
        (name, 52, (255, 255, 255)),
        (f'{n_gal} ordinary gallery photos  ->  {n_kept} studio angles', 32, (170, 190, 230)),
        (f'Background: {bgname}', 26, (120, 140, 180)),
    ])
    inputs += ['-loop', '1', '-t', '1.8', '-i', str(cpath)]
    filters.append(f'[{idx}:v]fps=24,scale=1280:720,setsar=1[v{idx}]'); idx += 1
    inputs += ['-i', str(ST / vin / 'studio_spin' / f'{bg}.mp4')]
    filters.append(f'[{idx}:v]fps=24,scale=1280:720:force_original_aspect_ratio=decrease,'
                   f'pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=0x0a0b0e,setsar=1[v{idx}]')
    idx += 1

concat = ''.join(f'[v{i}]' for i in range(idx)) + f'concat=n={idx}:v=1:a=0[out]'
fc = ';'.join(filters + [concat])
mp4 = BASE / 'rnd' / 'LOTVUE_SPYNE_CLONE_DEMO.mp4'
r = subprocess.run(['ffmpeg', '-y', *inputs, '-filter_complex', fc, '-map', '[out]',
                    '-c:v', 'libx264', '-crf', '19', '-pix_fmt', 'yuv420p',
                    '-loglevel', 'error', str(mp4)],
                   capture_output=True, text=True, timeout=600)
if r.returncode != 0:
    print('FAIL', r.stderr[:600])
else:
    print('OK', mp4, round(mp4.stat().st_size / 1e6, 1), 'MB')
