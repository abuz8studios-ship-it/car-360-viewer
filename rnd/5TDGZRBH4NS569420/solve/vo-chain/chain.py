"""VO-style ring chain: compose calibrated relative poses around the orbit,
propagate scale via shared keypoints, write a COLMAP TXT init model."""
import sqlite3, struct, math, sys
import numpy as np
import cv2
from pathlib import Path

DB = r"E:\ABU\360Car\rnd\5TDGZRBH4NS569420\solve\global-ring-cal\database.db"
OUT = Path(r"E:\ABU\360Car\rnd\5TDGZRBH4NS569420\solve\vo-chain\init")
OUT.mkdir(parents=True, exist_ok=True)

con = sqlite3.connect(DB)
cam_id, model, W, H, params = con.execute("select camera_id,model,width,height,params from cameras").fetchone()
p = struct.unpack(f"<{len(params)//8}d", params)
f, cx, cy, k = p
K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1]])

names = dict(con.execute("select image_id,name from images"))
id_by_frame = {int(n.split("_")[1].split(".")[0]): iid for iid, n in names.items()}

kps = {}
for iid, rows, cols, data in con.execute("select image_id,rows,cols,data from keypoints"):
    a = np.frombuffer(data, np.float32).reshape(rows, cols)
    kps[iid] = a[:, :2].copy()

def pair_id(i, j):
    return i * 2147483647 + j if i <= j else j * 2147483647 + i

def get_pair(i, j):
    """Return matches (idx_i, idx_j) and relative pose j_from_i."""
    pid = pair_id(i, j)
    row = con.execute("select rows,data,qvec,tvec,config from two_view_geometries where pair_id=?", (pid,)).fetchone()
    if row is None or row[0] == 0:
        return None
    rows, data, qv, tv, cfg = row
    m = np.frombuffer(data, np.uint32).reshape(rows, 2)
    if i > j:
        m = m[:, ::-1]
    q = np.frombuffer(qv, np.float64) if qv else None
    t = np.frombuffer(tv, np.float64) if tv else None
    R = None
    if q is not None and len(q) == 4 and np.isfinite(q).all():
        w, x, y, z = q
        R = np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                      [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                      [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])
    if i > j and R is not None:
        R, t = R.T, -R.T @ t  # stored j_from_i for pid order; invert
    return m, R, (t if t is not None else None)

def rel_pose_cv(i, j, m):
    """Fallback/refine: essential from inlier matches."""
    p1 = kps[i][m[:, 0]]; p2 = kps[j][m[:, 1]]
    E, mask = cv2.findEssentialMat(p1, p2, K, method=cv2.RANSAC, prob=0.9999, threshold=1.5)
    _, R, t, mask2 = cv2.recoverPose(E, p1, p2, K, mask=mask)
    return R, t.ravel(), (mask2.ravel() > 0)

def triangulate(Ri, ti, Rj, tj, pi, pj):
    Pi = K @ np.hstack([Ri, ti.reshape(3, 1)])
    Pj = K @ np.hstack([Rj, tj.reshape(3, 1)])
    X = cv2.triangulatePoints(Pi, Pj, pi.T, pj.T)
    X = (X[:3] / X[3]).T
    return X

ring = [1, 2, 3, 4, 5, 6, 7, 8]
# world = cam of frame 1
poses = {id_by_frame[1]: (np.eye(3), np.zeros(3))}
prev_depths = None  # keypoint_idx -> depth in current frame's camera
report = []
for a, b in zip(ring, ring[1:]):
    ia, ib = id_by_frame[a], id_by_frame[b]
    got = get_pair(ia, ib)
    m, Rdb, tdb = got
    R, t, inl = rel_pose_cv(ia, ib, m)
    m_in = m[inl]
    Ra, ta = poses[ia]
    # depths of matched pts in cam a (unit-scale pair)
    X = triangulate(np.eye(3), np.zeros(3), R, t, kps[ia][m_in[:, 0]], kps[ib][m_in[:, 1]])
    d_a = X[:, 2]
    ok = d_a > 0
    scale = 1.0
    if prev_depths is not None:
        shared = [(idx, d) for idx, d in zip(m_in[:, 0], d_a) if idx in prev_depths and d > 0]
        if len(shared) >= 3:
            ratios = [prev_depths[idx] / d for idx, d in shared]
            scale = float(np.median(ratios))
            report.append(f"{a}->{b}: {len(m_in)} inl, {len(shared)} shared, scale {scale:.3f}")
        else:
            report.append(f"{a}->{b}: {len(m_in)} inl, only {len(shared)} shared, scale kept 1.0")
    else:
        report.append(f"{a}->{b}: {len(m_in)} inl, first pair, scale 1.0")
    t_s = t * scale
    Rb = R @ Ra
    tb = R @ ta + t_s
    poses[ib] = (Rb, tb)
    # depths of these points in cam b for next link
    Xb = (R @ (X * scale).T).T + t_s
    prev_depths = {int(idx): float(z) for idx, z in zip(m_in[:, 1], Xb[:, 2]) if z > 0}

# loop closure check 8->1
got = get_pair(id_by_frame[8], id_by_frame[1])
if got:
    m, _, _ = got
    R81, t81, inl = rel_pose_cv(id_by_frame[8], id_by_frame[1], m)
    R8, t8 = poses[id_by_frame[8]]
    R1c = R81 @ R8
    ang = math.degrees(math.acos(max(-1, min(1, (np.trace(R1c) - 1) / 2))))
    report.append(f"loop closure rotation error: {ang:.1f} deg (0 = perfect)")

MODEL_NAMES = {0: "SIMPLE_PINHOLE", 1: "PINHOLE", 2: "SIMPLE_RADIAL", 3: "RADIAL", 4: "OPENCV"}
(OUT / "cameras.txt").write_text(f"{cam_id} {MODEL_NAMES[model]} {W} {H} " + " ".join(f"{v:.6f}" for v in p) + "\n")

def rot2q(R):
    w = math.sqrt(max(0, 1 + R[0,0] + R[1,1] + R[2,2])) / 2
    if w > 1e-6:
        return w, (R[2,1]-R[1,2])/(4*w), (R[0,2]-R[2,0])/(4*w), (R[1,0]-R[0,1])/(4*w)
    return 1, 0, 0, 0

lines = []
for iid, (R, t) in poses.items():
    q = rot2q(R)
    lines.append(f"{iid} {q[0]:.9f} {q[1]:.9f} {q[2]:.9f} {q[3]:.9f} {t[0]:.6f} {t[1]:.6f} {t[2]:.6f} {cam_id} {names[iid]}")
    lines.append("")
(OUT / "images.txt").write_text("\n".join(lines) + "\n")
(OUT / "points3D.txt").write_text("")
print("\n".join(report))
# geometry summary
cs = {names[iid]: -R.T @ t for iid, (R, t) in poses.items()}
cen = np.mean(list(cs.values()), axis=0)
for n in sorted(cs):
    v = cs[n] - cen
    print(f"{n} r={np.linalg.norm(v):5.2f} az={math.degrees(math.atan2(v[0], v[2])):7.1f}")
