import subprocess, time

SERVICE_DIR = "C:\\Program Files (x86)\\cloudflared"
TUNNEL_ID = "b9aa6d6c-bfce-4286-9a7a-ae2c043d48d7"
CREDS_FILE = f"{SERVICE_DIR}\\{TUNNEL_ID}.json"
CONFIG_FILE = f"{SERVICE_DIR}\\config.yml"

config = f"""tunnel: {TUNNEL_ID}
credentials-file: {CREDS_FILE}

ingress:
  - hostname: lotvue.abuz8ai.com
    service: http://127.0.0.1:8001
  - service: http_status:404
"""

with open(CONFIG_FILE, 'w') as f:
    f.write(config)

print("Config written:")
with open(CONFIG_FILE) as f:
    print(f.read())

# Kill cloudflared and restart service
subprocess.run(['taskkill', '/F', '/IM', 'cloudflared.exe'], capture_output=True)
time.sleep(3)
r = subprocess.run(['sc.exe', 'start', 'cloudflared'], capture_output=True, text=True)
print("Start:", r.stdout.strip())
time.sleep(8)
r = subprocess.run(['sc.exe', 'query', 'cloudflared'], capture_output=True, text=True)
for line in r.stdout.splitlines():
    if 'STATE' in line:
        print("State:", line.strip())

# Test local app
import urllib.request
try:
    resp = urllib.request.urlopen('http://127.0.0.1:8001/', timeout=5)
    print(f"Local app: HTTP {resp.getcode()} OK")
except Exception as e:
    print(f"Local app: {e}")

print("Done.")
