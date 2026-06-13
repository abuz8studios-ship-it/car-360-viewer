"""
Sanitize the assembled portable bundle into a CLEAN, fresh, GLO3D-style program:
  - fresh vehicles.db: schema + backgrounds kept, ALL demo/personal data wiped,
    ONE master account inserted
  - clean .env: no live Stripe/Gmail/webhook secrets; auth on, payments off
  - empty storage/ (no demo cars / photos)

Run AFTER assemble_portable.py.  Master login is printed at the end.
"""
import sqlite3
import shutil
import hashlib
from pathlib import Path

BUNDLE_APP = Path(r"E:\ABU\360Car\dist\LotVue360_standalone\app")
DB = BUNDLE_APP / "vehicles.db"

# ---- master account (the only account in a fresh install) ----
MASTER_EMAIL = "admin@lotvue360.app"
MASTER_PASS = "Abuz809!"
MASTER_NAME = "Master Admin"
MASTER_DEALER = "mylot"

if not DB.exists():
    raise SystemExit(f"No DB at {DB}. Run assemble_portable.py first.")

conn = sqlite3.connect(str(DB))
# wipe every data table; keep schema + backgrounds library
for tbl in ("vehicles", "leads", "users", "dealers", "hotspots", "gallery_photos",
            "interior_panoramas", "payments", "credit_log", "post_log",
            "reviews", "follow_ups", "analytics", "appointments"):
    try:
        conn.execute(f"DELETE FROM {tbl}")
    except sqlite3.OperationalError:
        pass  # table not present in this build

# insert the single master account
pw = hashlib.sha256(MASTER_PASS.encode()).hexdigest()
conn.execute(
    "INSERT INTO users (email, name, password_hash, plan, credits, created_at) "
    "VALUES (?,?,?,?,?,datetime('now'))",
    (MASTER_EMAIL, MASTER_NAME, pw, "dealership", 999999))
try:
    conn.execute(
        "INSERT INTO dealers (name, display_name, email, plan, brand_color, created_at) "
        "VALUES (?,?,?,?,?,datetime('now'))",
        (MASTER_DEALER, "My Dealership", MASTER_EMAIL, "dealership", "#2E75B6"))
except sqlite3.OperationalError:
    pass
conn.commit()
conn.execute("VACUUM")
conn.commit()
conn.close()

# ---- clean .env (no secrets) ----
(BUNDLE_APP / ".env").write_text(
    "# LotVue360 - clean install\n"
    "LOTVUE_FEATURE_AUTH=1\n"
    "LOTVUE_FEATURE_PAYMENTS=0\n"
    "LOTVUE_FEATURE_WATERMARK=0\n"
    "LOTVUE_RENDER_MODE=natural\n"
    "LOTVUE_PORT=8360\n"
    "LOTVUE_PUBLIC_URL=http://127.0.0.1:8360\n"
    "LOTVUE_OLLAMA_MODEL=qwen3.6-64k:latest\n"
    "COMFYUI_URL=http://127.0.0.1:8188\n",
    encoding="utf-8")

# ---- empty storage (no demo cars/photos) ----
storage = BUNDLE_APP / "storage"
if storage.exists():
    shutil.rmtree(storage, ignore_errors=True)
storage.mkdir(parents=True, exist_ok=True)

# ---- drop any stray secret/log files from the bundle ----
for junk in ("server_stdout.log", "server_stderr.log", "standalone.log",
             "standalone_stdio.log", "desktop_launch.log", ".jwt_secret",
             "vehicles.db.bak"):
    p = BUNDLE_APP / junk
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass

print("CLEAN BUILD READY")
print(f"  master email:    {MASTER_EMAIL}")
print(f"  master password: {MASTER_PASS}")
print("  data wiped, backgrounds kept, secrets removed, storage emptied")
