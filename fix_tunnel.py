"""
Fix cloudflared service config — writes correct tunnel config to service directory
and restarts the service cleanly.
"""
import subprocess, time, os

TUNNEL_ID = "b9aa6d6c-bfce-4286-9a7a-ae2c043d48d7"
SERVICE_DIR = r"C:\Program Files (x86)\cloudflared"
USER_DIR = r"~\.cloudflared"

# Write the correct full config to the service directory
config_content = f"""tunnel: {TUNNEL_ID}
credentials-file: {SERVICE_DIR}\\{TUNNEL_ID}.json

ingress:
  - hostname: lotvue.abuz8ai.com
    service: http://127.0.0.1:8001
  - service: http_status:404
"""

config_path = os.path.join(SERVICE_DIR, "config.yml")
with open(config_path, 'w') as f:
    f.write(config_content)
print(f"Config written to: {config_path}")
print("Content:")
print(config_content)

# Force kill any stuck cloudflared processes
subprocess.run(['taskkill', '/F', '/IM', 'cloudflared.exe'], capture_output=True)
time.sleep(3)

# Start the service
result = subprocess.run(['sc.exe', 'start', 'cloudflared'], capture_output=True, text=True)
print("Start result:", result.stdout.strip() or result.stderr.strip())
time.sleep(6)

# Check state
result = subprocess.run(['sc.exe', 'query', 'cloudflared'], capture_output=True, text=True)
for line in result.stdout.splitlines():
    if 'STATE' in line:
        print("Service state:", line.strip())

# Quick local test
import urllib.request
try:
    r = urllib.request.urlopen('http://127.0.0.1:8001/', timeout=5)
    print(f"Local app: HTTP {r.getcode()} - OK")
except Exception as e:
    print(f"Local app: FAIL - {e}")

print("Done.")
