from PIL import Image, ImageDraw, ImageFilter
import math, shutil
from pathlib import Path

OUT = Path(r".\360Car\app\storage\DEMO360")
EXT = OUT / "exterior"
SPIN = OUT / "spin"

W, H = 1280, 720
NUM = 36
BG = (10, 22, 40)
FLOOR = (13, 26, 48)

def lerp(c1, c2, t):
    return tuple(int(c1[i]+(c2[i]-c1[i])*t) for i in range(3))

def render(idx, total):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    a = idx / total
    fy = int(H * 0.62)
    
    # Floor
    for y in range(fy, H):
        t = (y-fy)/(H-fy)
        d.line([(0,y),(W,y)], fill=lerp(FLOOR, BG, t*0.5))
    d.line([(0,fy),(W,fy)], fill=(26,46,80), width=1)
    
    # Turntable rings
    rcx, rcy = W//2, fy+30
    d.ellipse([rcx-350,rcy-40,rcx+350,rcy+40], outline=(201,168,76,60), width=2)
    d.ellipse([rcx-320,rcy-36,rcx+320,rcy+36], outline=(26,138,122,40), width=1)
    
    # Car body
    s = 420
    shade = 0.7 + 0.3*max(0, math.cos((a-0.15)*math.pi*2))
    cc = tuple(int(c*shade) for c in (170,178,190))
    dc = tuple(int(c*shade*0.6) for c in (170,178,190))
    gc = (int(20+30*shade), int(35+40*shade), int(50+50*shade))
    
    bw = s*(0.65+0.35*abs(math.sin(a*math.pi)))
    bh = s*0.28
    cx, cy = W//2, fy-20
    by = cy+bh*0.3
    
    d.rounded_rectangle([cx-bw,by-bh,cx+bw,by], radius=int(s*0.05), fill=cc, outline=dc, width=1)
    
    # Hood
    hw = bw*0.3
    d.polygon([(cx+bw-hw*0.1,by-bh),(cx+bw+hw*0.3,by-bh*0.85),(cx+bw+hw*0.3,by),(cx+bw,by)], fill=cc, outline=dc)
    
    # Trunk
    tw = bw*0.2
    d.polygon([(cx-bw+tw*0.1,by-bh),(cx-bw-tw*0.2,by-bh*0.9),(cx-bw-tw*0.2,by),(cx-bw,by)], fill=cc, outline=dc)
    
    # Cabin
    cw, ch = bw*0.55, bh*0.75
    pts = [(cx-cw*0.7,by-bh),(cx-cw*0.5,by-bh-ch),(cx+cw*0.3,by-bh-ch),(cx+cw*0.6,by-bh)]
    d.polygon(pts, fill=gc, outline=(40,60,80))
    for i in range(len(pts)):
        d.line([pts[i], pts[(i+1)%len(pts)]], fill=(180,190,200), width=2)
    
    # Wheels
    wr = int(s*0.085)
    wy = int(by+wr*0.1)
    for wx in [int(cx+bw*0.6), int(cx-bw*0.55)]:
        d.ellipse([wx-wr,wy-wr,wx+wr,wy+wr], fill=(30,30,30), outline=(60,60,60), width=2)
        rr = int(wr*0.6)
        d.ellipse([wx-rr,wy-rr,wx+rr,wy+rr], fill=(100,105,110), outline=(140,145,150))
        hr = int(wr*0.15)
        d.ellipse([wx-hr,wy-hr,wx+hr,wy+hr], fill=(160,165,170))
    
    # Headlights
    if a < 0.6 or a > 0.9:
        hx = int(cx+bw+bw*0.25)
        hy = int(by-bh*0.5)
        hw2 = int(s*0.04)
        d.ellipse([hx-hw2,hy-hw2//2,hx+hw2,hy+hw2//2], fill=(255,255,220))
    
    # Taillights
    if a > 0.4:
        tx = int(cx-bw-bw*0.18)
        ty = int(by-bh*0.5)
        tw2 = int(s*0.03)
        d.ellipse([tx-tw2,ty-tw2//2,tx+tw2,ty+tw2//2], fill=(220,30,30))
    
    # Reflection strip
    hy1, hy2 = int(by-bh*0.6), int(by-bh*0.55)
    d.rectangle([cx-bw*0.8,hy1,cx+bw*0.8,hy2], fill=tuple(min(255,c+40) for c in cc))
    
    # Shadow
    for sy in range(fy+5, fy+50):
        t = (sy-fy-5)/45
        d.line([(cx-250,sy),(cx+250,sy)], fill=(max(0,BG[0]-3),max(0,BG[1]-3),max(0,BG[2]-3)))
    
    img = img.filter(ImageFilter.GaussianBlur(radius=0.5))
    img.save(str(EXT / f"frame_{idx+1:04d}.jpg"), "JPEG", quality=92)

print("Generating 36 frames...")
for i in range(NUM):
    render(i, NUM)
print(f"Done: {len(list(EXT.glob('*.jpg')))}")

for i in range(NUM):
    src = EXT / f"frame_{i+1:04d}.jpg"
    dst = SPIN / f"spin_{i+1:04d}.jpg"
    if src.exists(): shutil.copy2(str(src), str(dst))

shutil.copy2(str(EXT/"frame_0004.jpg"), str(OUT/"thumbnail.jpg"))
