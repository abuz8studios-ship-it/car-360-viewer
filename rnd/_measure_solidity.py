import sys
from pathlib import Path
import numpy as np
import cv2
from PIL import Image
BASE = Path(r'E:\ABU\01_PRODUCTS\360Car-LotVue\app')
sys.path.insert(0, str(BASE))
from studio import cut_car
imp = BASE / r'storage\WDDEJ7EB9CA029569\_import'
# p_0001/p_0002 = doors-open (sheet row1 col1-2); p_0005/p_0013/p_0023/p_0030 = clean (score .992+)
for n in ('p_0001.jpg','p_0002.jpg','p_0005.jpg','p_0013.jpg','p_0023.jpg','p_0030.jpg'):
    img = Image.open(imp / n).convert('RGB')
    m = (np.array(cut_car(img).getchannel('A')) > 24).astype(np.uint8)
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    c = max(cnts, key=cv2.contourArea)
    sol = cv2.contourArea(c) / max(1.0, cv2.contourArea(cv2.convexHull(c)))
    print(n, round(sol, 3))
