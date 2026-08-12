"""vo-ring init: chained pairwise rotations (measured) + orbit-projected positions."""
import sqlite3, struct, math
import numpy as np
import cv2
from pathlib import Path

DB = r"E:\ABU\360Car\rnd\5TDGZRBH4NS569420\solve\global-ring-cal\database.db"
OUT = Path(r"E:\ABU\360Car\rnd\5TDGZRBH4NS569420\solve\vo-chain\init2")
OUT.mkdir(parents=True, exist_ok=True)
D = 4.5

con = sqlite3.connect(DB)
cam_id, model, W, H, params = con.execute("select camera_id,model,width,height,params from cameras").fetchone()
p = struct.unpack(f"<{len(params)//8}d", params)
f, cx, cy, k = p
K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1]])
names = dict(con.execute("select image_id,name from images"))
id_by_frame = {int(n.split("_")[1].split(".")[0]): iid for iid, n in names.items()}
kps = {iid: np.frombuffer(d, np.float32).reshape(r, c)[:, :2].copy()
       for iid, r, c, d in con.execute("select image_id,rows,cols,data from keypoints")}

def get_matches(i, j):
    pid = i * 2147483647 + j if i <= j else j * 2147483647 + i
    row = con.execute("select rows,data from two_view_geometries where pair_id=?", (pid,)).fetchone()
    if not row or row[0] == 0:
        return None
    m = np.frombuffer(row[1], np.uint32).reshape(row[0], 2)
    return m[:, ::-1] if i > j else m

ring = [1, 2, 3, 4, 5, 6, 7, 8]
Rw = {id_by_frame[1]: np.eye(3)}
for a, b in zip(ring, ring[1:]):
    ia, ib = id_by_frame[a], id_by_frame[b]
    m = get_matches(ia, ib)
    p1, p2 = kps[ia][m[:, 0]], kps[ib][m[:, 1]]
    E, mask = cv2.findEssentialMat(p1, p2, K, method=cv2.RANSAC, prob=0.9999, threshold=1.5)
    _, R, t, _ = cv2.recoverPose(E, p1, p2, K, mask=mask)
    Rw[ib] = R @ Rw[ia]
    ang = math.degrees(math.acos(max(-1, min(1, (np.trace(R) - 1) / 2))))
    print(f"{a}->{b}: rel-rot {ang:.1f} deg")

MODEL_NAMES = {0: "SIMPLE_PINHOLE", 1: "PINHOLE", 2: "SIMPLE_RADIAL", 3: "RADIAL", 4: "OPENCV"}
(OUT / "cameras.txt").write_text(f"{cam_id} {MODEL_NAMES[model]} {W} {H} " + " ".join(f"{v:.6f}" for v in p) + "\n")

def rot2q(R):
    w = math.sqrt(max(0, 1 + R[0,0] + R[1,1] + R[2,2])) / 2
    if w > 1e-6:
        return w, (R[2,1]-R[1,2])/(4*w), (R[0,2]-R[2,0])/(4*w), (R[1,0]-R[0,1])/(4*w)
    return 1, 0, 0, 0

lines = []
for iid, R in Rw.items():
    look = R.T @ np.array([0, 0, 1.0])   # viewing dir in world
    C = -D * look                         # camera on orbit, looking at origin
    t = -R @ C
    q = rot2q(R)
    lines.append(f"{iid} {q[0]:.9f} {q[1]:.9f} {q[2]:.9f} {q[3]:.9f} {t[0]:.6f} {t[1]:.6f} {t[2]:.6f} {cam_id} {names[iid]}")
    lines.append("")
(OUT / "images.txt").write_text("\n".join(lines) + "\n")
(OUT / "points3D.txt").write_text("")
# summary
cs = {names[iid]: -R.T @ (-R @ (-D * (R.T @ np.array([0,0,1.0])))) for iid, R in Rw.items()}
cen = np.mean(list(cs.values()), axis=0)
for n in sorted(cs):
    v = cs[n] - cen
    print(f"{n} r={np.linalg.norm(v):5.2f} az={math.degrees(math.atan2(v[0], v[2])):7.1f}")
print("wrote", OUT)
