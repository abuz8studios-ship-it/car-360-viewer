[CmdletBinding()]
param(
    [switch]$RemoveUserData,
    [switch]$RemoveDesktopShortcut
)

$ErrorActionPreference = 'Stop'
$InstallPath = Join-Path $env:LOCALAPPDATA 'Programs\LotVue360'
$DataPath = Join-Path $env:LOCALAPPDATA 'LotVue360'
$StartMenuPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\LotVue360'
$DesktopShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'LotVue360.lnk'

if (Test-Path -LiteralPath $InstallPath) {
    Remove-Item -LiteralPath $InstallPath -Recurse -Force
}
if (Test-Path -LiteralPath $StartMenuPath) {
    Remove-Item -LiteralPath $StartMenuPath -Recurse -Force
}
if ($RemoveDesktopShortcut -and (Test-Path -LiteralPath $DesktopShortcutPath)) {
    Remove-Item -LiteralPath $DesktopShortcutPath -Force
}
if ($RemoveUserData -and (Test-Path -LiteralPath $DataPath)) {
    Remove-Item -LiteralPath $DataPath -Recurse -Force
} else {
    Write-Host "Kept your user data and credentials at $DataPath"
    Write-Host 'To permanently remove that folder too, run with -RemoveUserData.'
}
Write-Host 'LotVue360 program files and Start Menu shortcuts were removed.'
