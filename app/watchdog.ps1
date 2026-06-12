# LotVue360 Watchdog — keeps uvicorn alive
# Runs as a background scheduled task, checks every 30 seconds

$PYTHON   = "E:\Miniconda3\envs\lotvue\python.exe"
$WORKDIR  = "E:\ABU\360Car\app"
$STDOUT   = "E:\ABU\360Car\app\server_stdout.log"
$STDERR   = "E:\ABU\360Car\app\server_stderr.log"
$PORT     = 8001
$LOG      = "E:\ABU\360Car\app\watchdog.log"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts  $msg" | Out-File -FilePath $LOG -Append -Encoding UTF8
}

Write-Log "Watchdog started"

while ($true) {
    # Check if anything is listening on port 8001
    $listening = netstat -ano | Select-String ":$PORT\s.*LISTENING"

    if (-not $listening) {
        Write-Log "Server DOWN — restarting uvicorn..."
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
            Write-Log "Server FAILED to restart — check $STDERR"
        }
    }

    Start-Sleep -Seconds 30
}
