"""Measure (1) subject contrast on Accord kept frames to find the ghost's
signature, (2) clip_fullcar scores on Mercedes kept frames to place the
whole-car threshold. Prints numbers; thresholds get set FROM these."""
import json, shutil, subprocess, sys, tempfile
from pathlib import Path
import numpy as np
from PIL import Image

BASE = Path(r'E:\ABU\01_PRODUCTS\360Car-LotVue\app')
sys.path.insert(0, str(BASE))
from studio import cut_car

def subject_stats(p):
    img = Image.open(p).convert('RGB')
    m = np.array(cut_car(img).getchannel('A')) > 24
    a = np.asarray(img, dtype=np.float32)
    if not m.any():
        return 0.0, 0.0
    px = a[m]
    std = float(px.std())
    sat = float((px.max(axis=1) - px.min(axis=1)).mean())
    return round(std, 1), round(sat, 1)

# Accord: which _import frames were the 11 kept? meta.json has clip_categories.
meta = json.loads((BASE / r'storage\1HGCV1F31MA037775\studio_spin\meta.json').read_text())
imp = BASE / r'storage\1HGCV1F31MA037775\_import'
cats = meta['clip_categories']
kept = [k for k, v in cats.items() if v == 'exterior']
print('== ACCORD subject contrast (std, sat) ==')
name_map = {f'p_{i+1:04d}.jpg': p for i, p in enumerate(sorted(
    x for x in imp.iterdir() if x.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')))}
for k in kept:
    src = name_map[k]
    print(k, src.name, subject_stats(src))

# Mercedes: fullcar scores for its kept frames
meta_m = json.loads((BASE / r'storage\WDDEJ7EB9CA029569\studio_spin\meta.json').read_text())
imp_m = BASE / r'storage\WDDEJ7EB9CA029569\_import'
cats_m = meta_m['clip_categories']
kept_m = [k for k, v in cats_m.items() if v == 'exterior']
nm_m = {f'p_{i+1:04d}.jpg': p for i, p in enumerate(sorted(
    x for x in imp_m.iterdir() if x.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')))}
tmp = Path(tempfile.mkdtemp(prefix='lv_meas_'))
inv = {}
for i, k in enumerate(kept_m):
    n = f'p_{i+1:04d}.jpg'
    shutil.copyfile(nm_m[k], tmp / n)
    inv[n] = nm_m[k].name
r = subprocess.run([r'E:\Miniconda3\envs\lotvue\python.exe', '-s',
                    str(BASE / 'clip_fullcar.py'), str(tmp)],
                   capture_output=True, text=True, timeout=900)
d = json.loads(r.stdout.strip().splitlines()[-1])
print('== MERC fullcar scores ==')
for ph in sorted(d['photos'], key=lambda x: x['score']):
    print(inv[ph['file']], ph['score'])
shutil.rmtree(tmp, ignore_errors=True)
