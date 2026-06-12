"""
Full dealer-journey QA against the PUBLIC URL. Prints PASS/FAIL per step.
Run:  E:\\Miniconda3\\envs\\lotvue\\python.exe journey_qa.py [base_url]
"""
import io
import sys
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://lotvue.abuz8ai.com").rstrip("/")
ASSETS = Path(r"E:\ABU\360Car\test_assets")
VIN = "2HGFC2F69KH500111"  # valid-format test VIN (2019 Honda Civic)
EMAIL = "journey.dealer@lotvue.test"
PASSWORD = "Journey1234!"
TOKEN = ""

results = []

# Cloudflare bot-fight blocks the default Python-urllib UA with 403
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LotVueQA/1.0"
_opener = urllib.request.build_opener()
_opener.addheaders = [("User-Agent", _UA)]
urllib.request.install_opener(_opener)


def step(name, fn):
    try:
        out = fn()
        results.append((name, True, out))
        print(f"PASS  {name}: {out}")
    except Exception as e:
        results.append((name, False, str(e)))
        print(f"FAIL  {name}: {e}")


def _auth_headers(extra=None):
    h = dict(extra or {})
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def post_form(path, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def post_json(path, obj):
    req = urllib.request.Request(f"{BASE}{path}", data=json.dumps(obj).encode(),
                                 headers=_auth_headers({"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def get(path, raw=False):
    req = urllib.request.Request(f"{BASE}{path}", headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read() if raw else json.loads(r.read())


def post_file(path, fields, file_field, file_path):
    boundary = "FormBoundaryJourneyQA"
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    fname = Path(file_path).name
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
                  f"filename=\"{fname}\"\r\nContent-Type: image/jpeg\r\n\r\n").encode())
    parts.append(Path(file_path).read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(f"{BASE}{path}", data=body,
                                 headers=_auth_headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}))
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


# 1. Register
step("register", lambda: post_form("/api/register", {"email": EMAIL, "name": "Journey Dealer", "password": PASSWORD}))

# 2. Login
step("login", lambda: post_form("/api/login", {"email": EMAIL, "password": PASSWORD}))

# 3. JWT — stored and used for every gated call from here on
def _get_token():
    global TOKEN
    TOKEN = post_json("/api/auth/token", {"email": EMAIL, "password": PASSWORD})["access_token"]
    return "token-len=" + str(len(TOKEN))
step("jwt_token", _get_token)

# 4. Create vehicle
step("create_vehicle", lambda: post_form("/api/create-vehicle",
     {"vin": VIN, "year": "2009", "make": "Toyota", "model": "Prius",
      "color": "Silver", "price": "6995", "mileage": "131000"}))

# 5. Upload 8 exterior photos
def upload_all():
    out = []
    for i, f in enumerate(sorted(ASSETS.glob("test_*.jpg"))):
        r = post_file("/api/upload/exterior", {"vin": VIN, "index": str(i)}, "photo", f)
        out.append(r.get("status", r))
    return f"{len(out)} uploaded"
step("upload_8_exterior", upload_all)

# 6. Finalize (background removal + turntable + align + interpolate)
step("finalize_start", lambda: post_json(f"/api/finalize/{VIN}", {}))

def wait_finalize():
    for _ in range(120):
        time.sleep(5)
        s = get(f"/api/finalize-status/{VIN}")
        if s.get("ready") or s.get("status") in ("done", "complete", "ready"):
            return s
        if s.get("status") == "error":
            raise RuntimeError(s)
    raise TimeoutError("finalize did not complete in 600s")
step("finalize_complete", wait_finalize)

# 7. Viewer renders publicly
def check_viewer():
    html = get(f"/view/{VIN}", raw=True).decode("utf-8", "replace")
    assert "og:image" in html, "no og tags"
    assert "switchTab" in html, "no tab bar"
    return f"viewer html {len(html)} bytes, og+tabs present"
step("public_viewer", check_viewer)

# 8. VIN decode
step("vin_decode", lambda: get(f"/api/vin/{VIN}/decode"))

# 9. Lead submit (triggers SMTP notify)
step("lead_submit", lambda: post_json("/api/leads", {
    "vin": VIN, "name": "Journey Buyer", "email": "buyer@example.com",
    "phone": "5550001111", "message": "Is this still available?", "wants_test_drive": 1}))

# 10. Exports
step("export_qr", lambda: f"{len(get(f'/api/vehicle/{VIN}/export/qr.png', raw=True))} bytes")
step("export_zip", lambda: f"{len(get(f'/api/vehicle/{VIN}/export/frames.zip', raw=True))} bytes")
step("export_gif", lambda: f"{len(get(f'/api/vehicle/{VIN}/export/spin.gif', raw=True))} bytes")

# 11. Checkout session (live mode — session only, no charge)
step("stripe_checkout", lambda: post_form("/api/create-checkout-session",
     {"plan_id": "starter", "user_email": EMAIL})["session_url"].split("?")[0][:40])

# 12. Syndication surfaces include the new car
def check_feeds():
    fb = get("/api/syndication/facebook-csv", raw=True).decode("utf-8", "replace")
    gx = get("/api/feeds/google-vehicle-listings.xml", raw=True).decode("utf-8", "replace")
    assert VIN in fb or VIN in gx, "VIN missing from feeds"
    return "VIN present in feeds"
step("feeds_include_vehicle", check_feeds)

print()
fails = [r for r in results if not r[1]]
print(f"=== JOURNEY: {len(results) - len(fails)}/{len(results)} PASS ===")
sys.exit(1 if fails else 0)
