# LotVue360 uptime watchdog — restarts the :8360 engine if it dies.
# Registered as scheduled task LOTVUE-Watchdog (every 5 min) + LOTVUE-Boot (at startup).
# Never touches cloudflared (runs as its own Windows service) and never kills by port.

$log = 'E:\ABU\360Car\app\watchdog.log'
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

$up = Test-NetConnection -ComputerName 127.0.0.1 -Port 8360 -WarningAction SilentlyContinue -InformationLevel Quiet
if ($up) {
    # healthy — also require an HTTP 200, not just an open socket
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8360/api/health' -TimeoutSec 15 -UseBasicParsing
        if ($r.StatusCode -eq 200) { exit 0 }
    } catch {
        Add-Content $log "$stamp PORT-OPEN-BUT-UNHEALTHY: $($_.Exception.Message)"
    }
}

Add-Content $log "$stamp DOWN -> restarting via start_server.bat"
Start-Process -FilePath 'E:\ABU\360Car\app\start_server.bat' -WorkingDirectory 'E:\ABU\360Car\app' -WindowStyle Hidden
Start-Sleep -Seconds 15
$up2 = Test-NetConnection -ComputerName 127.0.0.1 -Port 8360 -WarningAction SilentlyContinue -InformationLevel Quiet
Add-Content $log "$stamp restart result: port8360=$up2"
