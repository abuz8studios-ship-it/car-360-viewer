"""Synthesize a COLMAP TXT model: cameras on the known walkaround ring, looking at the car.
Usage: make_init.py <out_dir> <chirality:+1|-1>"""
import sys, math, sqlite3, struct
import numpy as np
from pathlib import Path

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
sign = float(sys.argv[2])  # +1 or -1 flips azimuth direction

DB = r"E:\ABU\360Car\rnd\5TDGZRBH4NS569420\solve\global-ring-cal\database.db"
AZ = {1: 0, 2: 35, 3: 90, 4: 140, 5: 180, 6: 225, 7: 270, 8: 320}
RADIUS, CAM_Y = 4.5, -0.55   # world: +Y down, car center at origin; camera ~0.55 above center

con = sqlite3.connect(DB)
cam = con.execute("select camera_id, model, width, height, params from cameras").fetchone()
cam_id, model, w, h, params = cam
p = struct.unpack(f"<{len(params)//8}d", params)
MODEL_NAMES = {0: "SIMPLE_PINHOLE", 1: "PINHOLE", 2: "SIMPLE_RADIAL", 3: "RADIAL", 4: "OPENCV"}
(out / "cameras.txt").write_text(
    f"# Camera list\n{cam_id} {MODEL_NAMES[model]} {w} {h} " + " ".join(f"{v:.6f}" for v in p) + "\n")

def rot2q(R):
    w = math.sqrt(max(0, 1 + R[0,0] + R[1,1] + R[2,2])) / 2
    if w > 1e-6:
        x = (R[2,1]-R[1,2])/(4*w); y = (R[0,2]-R[2,0])/(4*w); z = (R[1,0]-R[0,1])/(4*w)
    else:
        x, y, z = 1, 0, 0
    return w, x, y, z

lines = ["# Image list: IMAGE_ID QW QX QY QZ TX TY TZ CAMERA_ID NAME"]
for iid, name in con.execute("select image_id, name from images"):
    k = int(name.split("_")[1].split(".")[0])
    a = math.radians(sign * AZ[k])
    C = np.array([RADIUS*math.sin(a), CAM_Y, RADIUS*math.cos(a)])
    z = -C / np.linalg.norm(C)                       # look at origin
    down = np.array([0.0, 1.0, 0.0])
    x = np.cross(down, z); x /= np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.stack([x, y, z])                          # world->cam rows
    t = -R @ C
    q = rot2q(R)
    lines.append(f"{iid} {q[0]:.9f} {q[1]:.9f} {q[2]:.9f} {q[3]:.9f} "
                 f"{t[0]:.6f} {t[1]:.6f} {t[2]:.6f} {cam_id} {name}")
    lines.append("")  # empty 2D point line
(out / "images.txt").write_text("\n".join(lines) + "\n")
(out / "points3D.txt").write_text("# empty\n")
print("wrote", out, "sign", sign)
