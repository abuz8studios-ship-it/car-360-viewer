import sys, json
from pathlib import Path
import numpy as np, cv2
from PIL import Image
BASE = Path(r'E:\ABU\01_PRODUCTS\360Car-LotVue\app')
sys.path.insert(0, str(BASE))
from studio import cut_car

def edge_energy(p):
    img = Image.open(p).convert('L')
    m = np.array(cut_car(Image.open(p).convert('RGB')).getchannel('A')) > 24
    g = cv2.Sobel(np.asarray(img, dtype=np.float32), cv2.CV_32F, 1, 0)**2 + \
        cv2.Sobel(np.asarray(img, dtype=np.float32), cv2.CV_32F, 0, 1)**2
    if not m.any():
        return 0.0
    return round(float(np.sqrt(g[m]).mean()), 1)

acc = BASE / r'storage\1HGCV1F31MA037775\_import'
files_a = sorted(x for x in acc.iterdir() if x.suffix.lower() in ('.jpg','.jpeg','.png','.webp'))
ghost = files_a[38]  # p_0039 = 39th sorted file
print('ACCORD ghost', ghost.name, edge_energy(ghost))
out = BASE / r'storage\4S4BSENC1K3205956\_import'
files_o = sorted(x for x in out.iterdir() if x.suffix.lower() in ('.jpg','.jpeg','.png','.webp'))
meta = json.loads((BASE / r'storage\4S4BSENC1K3205956\studio_spin\meta.json').read_text())
kept = [k for k, v in meta['clip_categories'].items() if v == 'exterior']
nm = {f'p_{i+1:04d}.jpg': p for i, p in enumerate(files_o)}
for k in kept:
    print('OUTBACK', k, edge_energy(nm[k]))
