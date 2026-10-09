[CmdletBinding()]
param(
    [switch]$NoLaunch,
    [switch]$DesktopShortcut
)

$ErrorActionPreference = 'Stop'
$Source = $PSScriptRoot
$Executable = Join-Path $Source 'LotVue360.exe'
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "LotVue360.exe was not found beside this installer. Extract the full release ZIP first."
}

$Running = Get-Process -Name 'LotVue360' -ErrorAction SilentlyContinue
if ($Running) {
    throw 'LotVue360 is running. Exit it from the system tray and run the installer again.'
}

$ProgramsRoot = Join-Path $env:LOCALAPPDATA 'Programs'
$InstallPath = Join-Path $ProgramsRoot 'LotVue360'
$StagingPath = Join-Path $ProgramsRoot ('.LotVue360-install-' + [guid]::NewGuid().ToString('N'))
$DataPath = Join-Path $env:LOCALAPPDATA 'LotVue360'
$StartMenuPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\LotVue360'

New-Item -ItemType Directory -Path $ProgramsRoot -Force | Out-Null
New-Item -ItemType Directory -Path $DataPath -Force | Out-Null
Write-Host 'Installing LotVue360 for the current Windows user...'
try {
    New-Item -ItemType Directory -Path $StagingPath -Force | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | Copy-Item -Destination $StagingPath -Recurse -Force
    if (Test-Path -LiteralPath $InstallPath) {
        Remove-Item -LiteralPath $InstallPath -Recurse -Force
    }
    Move-Item -LiteralPath $StagingPath -Destination $InstallPath
} catch {
    if (Test-Path -LiteralPath $StagingPath) {
        Remove-Item -LiteralPath $StagingPath -Recurse -Force -ErrorAction SilentlyContinue
    }
    throw "Install failed. Close any running LotVue360 process and run this installer again. $($_.Exception.Message)"
}

New-Item -ItemType Directory -Path $StartMenuPath -Force | Out-Null
$WshShell = New-Object -ComObject WScript.Shell
$StartShortcut = $WshShell.CreateShortcut((Join-Path $StartMenuPath 'LotVue360.lnk'))
$StartShortcut.TargetPath = Join-Path $InstallPath 'LotVue360.exe'
$StartShortcut.WorkingDirectory = $InstallPath
$StartShortcut.IconLocation = Join-Path $InstallPath 'LotVue360.exe'
$StartShortcut.Description = 'LotVue360 local dealer photo and inventory studio'
$StartShortcut.Save()

$UninstallScript = Join-Path $InstallPath 'Uninstall-LotVue360.ps1'
if (Test-Path -LiteralPath $UninstallScript -PathType Leaf) {
    $UninstallShortcut = $WshShell.CreateShortcut((Join-Path $StartMenuPath 'Uninstall LotVue360.lnk'))
    $UninstallShortcut.TargetPath = 'powershell.exe'
    $UninstallShortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$UninstallScript`""
    $UninstallShortcut.WorkingDirectory = $InstallPath
    $UninstallShortcut.Description = 'Remove LotVue360; user data is kept by default'
    $UninstallShortcut.Save()
}

if ($DesktopShortcut) {
    $Desktop = [Environment]::GetFolderPath('Desktop')
    $DesktopLink = $WshShell.CreateShortcut((Join-Path $Desktop 'LotVue360.lnk'))
    $DesktopLink.TargetPath = Join-Path $InstallPath 'LotVue360.exe'
    $DesktopLink.WorkingDirectory = $InstallPath
    $DesktopLink.IconLocation = Join-Path $InstallPath 'LotVue360.exe'
    $DesktopLink.Description = 'LotVue360 local dealer photo and inventory studio'
    $DesktopLink.Save()
}

Write-Host "Installed: $InstallPath"
Write-Host "User data: $DataPath (kept separately from program files)"
Write-Host "Start Menu: $StartMenuPath\LotVue360.lnk"
if (-not $NoLaunch) {
    Start-Process -FilePath (Join-Path $InstallPath 'LotVue360.exe') -WorkingDirectory $InstallPath
}
