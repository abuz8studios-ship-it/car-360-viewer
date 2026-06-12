<#
  Install-LotVue360.ps1 — installs the bundled LotVue360 desktop program.
  Copies the frozen app to %LOCALAPPDATA%\Programs\LotVue360, creates Start-Menu
  + Desktop shortcuts with the app icon, and (optionally) registers boot autostart.
  No admin required (installs per-user).

  Usage:   powershell -ExecutionPolicy Bypass -File Install-LotVue360.ps1
           ...-File Install-LotVue360.ps1 -NoAutostart
#>
param([switch]$NoAutostart)

$ErrorActionPreference = "Stop"
$srcDist   = Join-Path $PSScriptRoot "dist\LotVue360"
$installDir = Join-Path $env:LOCALAPPDATA "Programs\LotVue360"
$exe       = Join-Path $installDir "LotVue360.exe"
$icon      = Join-Path $installDir "static\desktop\lotvue.ico"

if (-not (Test-Path (Join-Path $srcDist "LotVue360.exe"))) {
  throw "Build not found at $srcDist. Run the PyInstaller build first (LotVue360.spec)."
}

Write-Host "Installing LotVue360 -> $installDir"
if (Test-Path $installDir) { Remove-Item $installDir -Recurse -Force }
Copy-Item $srcDist $installDir -Recurse -Force

$WshShell = New-Object -ComObject WScript.Shell

function New-Shortcut($path, $target, $icon) {
  $sc = $WshShell.CreateShortcut($path)
  $sc.TargetPath = $target
  $sc.WorkingDirectory = (Split-Path $target)
  if ($icon -and (Test-Path $icon)) { $sc.IconLocation = "$icon,0" }
  $sc.Description = "LotVue360 - 360 vehicle merchandising"
  $sc.Save()
}

# Desktop shortcut
$desktop = [Environment]::GetFolderPath("Desktop")
New-Shortcut (Join-Path $desktop "LotVue360.lnk") $exe $icon
Write-Host "  + Desktop shortcut"

# Start Menu shortcut
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\LotVue360"
New-Item -ItemType Directory -Force -Path $startMenu | Out-Null
New-Shortcut (Join-Path $startMenu "LotVue360.lnk") $exe $icon
Write-Host "  + Start Menu shortcut"

# Per-user uninstall registry entry (shows in Add/Remove Programs)
$uninstKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\LotVue360"
New-Item -Path $uninstKey -Force | Out-Null
Set-ItemProperty $uninstKey DisplayName "LotVue360"
Set-ItemProperty $uninstKey DisplayVersion "2.0.0"
Set-ItemProperty $uninstKey Publisher "ABUZ8 LLC"
Set-ItemProperty $uninstKey DisplayIcon $icon
Set-ItemProperty $uninstKey InstallLocation $installDir
Set-ItemProperty $uninstKey UninstallString "powershell -ExecutionPolicy Bypass -File `"$installDir\Uninstall-LotVue360.ps1`""
Write-Host "  + Add/Remove Programs entry"

# Drop an uninstaller alongside
@"
`$d = Join-Path `$env:LOCALAPPDATA 'Programs\LotVue360'
Remove-Item `$d -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path ([Environment]::GetFolderPath('Desktop')) 'LotVue360.lnk') -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path `$env:APPDATA 'Microsoft\Windows\Start Menu\Programs\LotVue360') -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\LotVue360' -Recurse -Force -ErrorAction SilentlyContinue
Remove-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'LotVue360' -ErrorAction SilentlyContinue
Write-Host 'LotVue360 uninstalled.'
"@ | Set-Content (Join-Path $installDir "Uninstall-LotVue360.ps1") -Encoding UTF8

# Autostart on login (per-user Run key) unless suppressed
if (-not $NoAutostart) {
  Set-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" "LotVue360" "`"$exe`""
  Write-Host "  + Autostart on login"
}

Write-Host ""
Write-Host "LotVue360 installed. Launch from the Desktop / Start Menu, or run:" -ForegroundColor Green
Write-Host "  $exe"
