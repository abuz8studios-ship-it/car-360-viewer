# LotVue360 Watchdog - keeps uvicorn alive
# Runs as a scheduled task (LOTVUE\Watchdog), checks every 30 seconds.
# NOTE: keep this file pure ASCII. Task Scheduler runs it via Windows PowerShell 5.1,
# which reads BOM-less .ps1 files as the ANSI codepage - non-ASCII chars (e.g. em-dash)
# get mangled and break parsing. Use '-' not a unicode dash.

$PYTHON   = "E:\Miniconda3\envs\lotvue\python.exe"
$WORKDIR  = "E:\ABU\360Car\app"
$STDOUT   = "E:\ABU\360Car\app\server_stdout.log"
$STDERR   = "E:\ABU\360Car\app\server_stderr.log"
$PORT     = 8360   # LotVue360 runs on :8360 (port 8001 is the Nemo brain / llama-server)
$LOG      = "E:\ABU\360Car\app\watchdog.log"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts  $msg" | Out-File -FilePath $LOG -Append -Encoding UTF8
}

Write-Log "Watchdog started"

while ($true) {
    # Check if anything is listening on the LotVue port ($PORT = 8360)
    $listening = netstat -ano | Select-String ":$PORT\s.*LISTENING"

    if (-not $listening) {
        Write-Log "Server DOWN - restarting uvicorn..."
        Start-Process -FilePath $PYTHON `
            -ArgumentList "-m uvicorn main:app --host 0.0.0.0 --port $PORT" `
            -WorkingDirectory $WORKDIR `
            -RedirectStandardOutput $STDOUT `
            -RedirectStandardError  $STDERR `
            -NoNewWindow
        Start-Sleep -Seconds 10
        $check = netstat -ano | Select-String ":$PORT\s.*LISTENING"
        if ($check) {
            Write-Log "Server RESTARTED successfully on port $PORT"
        } else {
            Write-Log "Server FAILED to restart - check $STDERR"
        }
    }

    Start-Sleep -Seconds 30
}
