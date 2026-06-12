"""
LOTVUE Persistence Setup
Creates two Windows Task Scheduler tasks:
1. LOTVUE\StartApp  — starts uvicorn on port 8001 at boot, auto-restarts on crash
2. LOTVUE\StartTunnel — starts cloudflared tunnel at boot (backup if service fails)
"""
import subprocess, os, sys

TASK_XML_APP = r"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>LOTVUE 360 Car Platform - Auto-start on boot</Description>
    <URI>\LOTVUE\StartApp</URI>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT15S</Delay>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>ABUZ8\wirec</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>E:\Miniconda3\envs\lotvue\python.exe</Command>
      <Arguments>-m uvicorn main:app --host 0.0.0.0 --port 8001</Arguments>
      <WorkingDirectory>E:\ABU\360Car\app</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""

TASK_XML_TUNNEL = r"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>LOTVUE Cloudflare Tunnel - Auto-start on boot</Description>
    <URI>\LOTVUE\StartTunnel</URI>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT20S</Delay>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>ABUZ8\wirec</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <RestartOnFailure>
      <Interval>PT2M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cloudflared</Command>
      <Arguments>tunnel --config C:\Users\wirec\.cloudflared\config.yml run lotvue</Arguments>
      <WorkingDirectory>C:\Users\wirec\.cloudflared</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""

# Write XML files
app_xml_path = r"C:\Temp\lotvue_app_task.xml"
tunnel_xml_path = r"C:\Temp\lotvue_tunnel_task.xml"

with open(app_xml_path, 'w', encoding='utf-16') as f:
    f.write(TASK_XML_APP)
print(f"Written: {app_xml_path}")

with open(tunnel_xml_path, 'w', encoding='utf-16') as f:
    f.write(TASK_XML_TUNNEL)
print(f"Written: {tunnel_xml_path}")

# Create LOTVUE folder in Task Scheduler
subprocess.run(['schtasks', '/Create', '/F', '/TN', r'LOTVUE\StartApp',
                '/XML', app_xml_path], capture_output=True, text=True)

result_app = subprocess.run(['schtasks', '/Create', '/F', '/TN', r'LOTVUE\StartApp',
                '/XML', app_xml_path], capture_output=True, text=True)
print("App task:", result_app.stdout.strip() or result_app.stderr.strip())

result_tunnel = subprocess.run(['schtasks', '/Create', '/F', '/TN', r'LOTVUE\StartTunnel',
                '/XML', tunnel_xml_path], capture_output=True, text=True)
print("Tunnel task:", result_tunnel.stdout.strip() or result_tunnel.stderr.strip())

# Also ensure cloudflared service is set to auto
subprocess.run(['sc.exe', 'config', 'cloudflared', 'start=', 'auto'], capture_output=True)
print("Cloudflared service set to auto-start")

# Start the LOTVUE app right now (don't wait for reboot)
import time
# Kill any existing instance first
subprocess.run(['taskkill', '/F', '/IM', 'python.exe', '/FI', 'WINDOWTITLE eq uvicorn*'],
               capture_output=True)
time.sleep(2)

# Start fresh
proc = subprocess.Popen(
    [r'E:\Miniconda3\envs\lotvue\python.exe', '-m', 'uvicorn', 'main:app',
     '--host', '0.0.0.0', '--port', '8001'],
    cwd=r'E:\ABU\360Car\app',
    stdout=open(r'C:\Temp\lotvue_app.log', 'w'),
    stderr=subprocess.STDOUT,
    creationflags=0x00000008  # DETACHED_PROCESS
)
print(f"LOTVUE app started, PID: {proc.pid}")
time.sleep(5)

# Verify port 8001 is listening
result = subprocess.run(['netstat', '-ano'], capture_output=True, text=True)
listening = [l for l in result.stdout.splitlines() if ':8001' in l and 'LISTEN' in l]
if listening:
    print("PORT 8001: LISTENING - OK")
else:
    print("PORT 8001: NOT FOUND - check logs")

print("Done. Both tasks registered. System will auto-start on next reboot.")
