[CmdletBinding()]
param(
    # Where ComfyUI and the WAN models go (about 40 GB). Use a roomy drive, e.g. D:\LotVue360-AI
    [string]$InstallDir = (Join-Path $env:USERPROFILE 'LotVue360-AI'),
    [switch]$NoStart
)
# Sets up LotVue360's "AI smooth" 360 spins on this NVIDIA PC: downloads the
# portable ComfyUI and the four WAN 2.2 first-last-frame model files, points
# LotVue360 at it, and adds a "Start LotVue360 AI Engine" shortcut. Safe to
# re-run: finished downloads are skipped and interrupted ones resume.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ComfyUrl = 'https://github.com/Comfy-Org/ComfyUI/releases/latest/download/ComfyUI_windows_portable_nvidia.7z'
$SevenZipUrl = 'https://www.7-zip.org/a/7zr.exe'
$HfBase = 'https://huggingface.co/Comfy-Org'
# Exact byte sizes from the Hugging Face repos, so a cut-off download is never used.
$Models = @(
    @{ Folder = 'diffusion_models'; Name = 'wan2.2_fun_inpaint_high_noise_14B_fp8_scaled.safetensors'; Size = 14294743520
       Url = "$HfBase/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/diffusion_models/wan2.2_fun_inpaint_high_noise_14B_fp8_scaled.safetensors" },
    @{ Folder = 'diffusion_models'; Name = 'wan2.2_fun_inpaint_low_noise_14B_fp8_scaled.safetensors'; Size = 14294743520
       Url = "$HfBase/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/diffusion_models/wan2.2_fun_inpaint_low_noise_14B_fp8_scaled.safetensors" },
    @{ Folder = 'text_encoders'; Name = 'umt5_xxl_fp8_e4m3fn_scaled.safetensors'; Size = 6735906897
       Url = "$HfBase/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors" },
    @{ Folder = 'vae'; Name = 'wan_2.1_vae.safetensors'; Size = 253815318
       Url = "$HfBase/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors" }
)

function Write-Step([string]$Message) { Write-Host ''; Write-Host "==> $Message" -ForegroundColor Cyan }

function Invoke-Download([string]$Url, [string]$Destination, [long]$ExpectedSize = 0) {
    if ((Test-Path -LiteralPath $Destination -PathType Leaf) -and
        ($ExpectedSize -eq 0 -or (Get-Item -LiteralPath $Destination).Length -eq $ExpectedSize)) {
        Write-Host "    already downloaded: $(Split-Path -Leaf $Destination)"
        return
    }
    $partial = "$Destination.part"
    # curl.exe ships with Windows 10/11; -C - resumes an interrupted download.
    & curl.exe -L --fail --retry 5 --retry-delay 10 -C - -o $partial $Url
    if ($LASTEXITCODE -ne 0) { throw "Download failed (curl exit $LASTEXITCODE): $Url. Run this setup again to resume." }
    if ($ExpectedSize -gt 0 -and (Get-Item -LiteralPath $partial).Length -ne $ExpectedSize) {
        throw "Download of $(Split-Path -Leaf $Destination) is incomplete. Run this setup again to resume."
    }
    Move-Item -LiteralPath $partial -Destination $Destination -Force
}

function Set-EnvValue([string]$Path, [string]$Key, [string]$Value) {
    $lines = @()
    if (Test-Path -LiteralPath $Path -PathType Leaf) { $lines = @([System.IO.File]::ReadAllLines($Path)) }
    $found = $false
    $lines = @($lines | ForEach-Object {
        if ($_ -match "^\s*$([regex]::Escape($Key))\s*=") { $found = $true; "$Key=$Value" } else { $_ }
    })
    if (-not $found) { $lines += "$Key=$Value" }
    # UTF-8 without a byte-order mark, which would corrupt the first key for the app
    [System.IO.File]::WriteAllLines($Path, [string[]]$lines)
}

function Find-SevenZip([string]$ToolsDir) {
    foreach ($candidate in @("$env:ProgramFiles\7-Zip\7z.exe", "${env:ProgramFiles(x86)}\7-Zip\7z.exe")) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $standalone = Join-Path $ToolsDir '7zr.exe'
    Invoke-Download $SevenZipUrl $standalone
    return $standalone
}

function Install-AISpin {
    Write-Step 'Checking the NVIDIA graphics card'
    $smi = Get-Command 'nvidia-smi.exe' -ErrorAction SilentlyContinue
    if (-not $smi) {
        throw 'No NVIDIA driver found (nvidia-smi.exe is missing). Install the latest driver from https://www.nvidia.com/drivers and run this again.'
    }
    $gpu = (& $smi.Source --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits | Select-Object -First 1)
    $name, $vramMb, $driver = ($gpu -split ',') | ForEach-Object { $_.Trim() }
    Write-Host "    $name, $([math]::Round([double]$vramMb / 1024)) GB video memory, driver $driver"
    if ([double]$vramMb -lt 12000) {
        Write-Warning 'Under 12 GB of video memory: AI spins will be very slow or may run out of memory. 16-24 GB is recommended.'
    }

    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    $drive = Get-PSDrive -Name ((Resolve-Path -LiteralPath $InstallDir).Drive.Name)
    $portable = Join-Path $InstallDir 'ComfyUI_windows_portable'
    $needGb = 40
    if (Test-Path -LiteralPath $portable) { $needGb = 37 }
    if ($drive.Free -lt $needGb * 1GB) {
        throw "Drive $($drive.Name): has $([math]::Round($drive.Free / 1GB)) GB free; about $needGb GB is needed. Re-run with -InstallDir on a bigger drive, e.g. -InstallDir D:\LotVue360-AI"
    }

    if (-not (Test-Path -LiteralPath (Join-Path $portable 'ComfyUI\main.py') -PathType Leaf)) {
        Write-Step 'Downloading ComfyUI for NVIDIA (about 2 GB)'
        $archive = Join-Path $InstallDir 'ComfyUI_windows_portable_nvidia.7z'
        Invoke-Download $ComfyUrl $archive
        Write-Step 'Unpacking ComfyUI'
        $sevenZip = Find-SevenZip $InstallDir
        & $sevenZip x $archive "-o$InstallDir" -y | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Unpacking ComfyUI failed (7-Zip exit $LASTEXITCODE)." }
        Remove-Item -LiteralPath $archive -Force
    } else {
        Write-Host '    ComfyUI is already installed'
    }

    Write-Step 'Downloading the WAN 2.2 AI models (about 35 GB; this takes a while)'
    $modelsDir = Join-Path $portable 'ComfyUI\models'
    foreach ($model in $Models) {
        $folder = Join-Path $modelsDir $model.Folder
        New-Item -ItemType Directory -Path $folder -Force | Out-Null
        Write-Host "    $($model.Name)"
        Invoke-Download $model.Url (Join-Path $folder $model.Name) $model.Size
    }

    Write-Step 'Creating the "Start LotVue360 AI Engine" shortcut'
    $launcher = Join-Path $portable 'Start-LotVue360-AI-Engine.bat'
    Set-Content -LiteralPath $launcher -Encoding ASCII -Value @(
        '@echo off',
        'title LotVue360 AI Engine (ComfyUI) - keep this window open while making AI spins',
        'cd /d "%~dp0"',
        '.\python_embeded\python.exe -s ComfyUI\main.py --listen 127.0.0.1 --port 8188 --disable-auto-launch',
        'pause'
    )
    $shell = New-Object -ComObject WScript.Shell
    $startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\LotVue360'
    New-Item -ItemType Directory -Path $startMenu -Force | Out-Null
    foreach ($folder in @($startMenu, [Environment]::GetFolderPath('Desktop'))) {
        $link = $shell.CreateShortcut((Join-Path $folder 'Start LotVue360 AI Engine.lnk'))
        $link.TargetPath = $launcher
        $link.WorkingDirectory = $portable
        $link.Description = 'Runs ComfyUI on the NVIDIA card for LotVue360 AI smooth 360 spins'
        $link.Save()
    }

    Write-Step 'Pointing LotVue360 at the AI engine'
    $dataDir = Join-Path $env:LOCALAPPDATA 'LotVue360'
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    $envFile = Join-Path $dataDir '.env'
    $template = Join-Path $PSScriptRoot 'app\.env.example'
    if (-not (Test-Path -LiteralPath $envFile) -and (Test-Path -LiteralPath $template)) {
        Copy-Item -LiteralPath $template -Destination $envFile
    }
    Set-EnvValue $envFile 'COMFYUI_URL' 'http://127.0.0.1:8188'
    Set-EnvValue $envFile 'COMFYUI_OUTPUT' (Join-Path $portable 'ComfyUI\output')
    Set-EnvValue $envFile 'LOTVUE_SPIN_GEN' '1'
    Write-Host "    updated $envFile"

    if ($NoStart) {
        Write-Step 'Done. Double-click "Start LotVue360 AI Engine" on the desktop before making AI spins.'
        return
    }
    Write-Step 'Starting the AI engine and checking it'
    Start-Process -FilePath $launcher -WorkingDirectory $portable
    $deadline = (Get-Date).AddMinutes(5)
    $up = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        try { Invoke-RestMethod -Uri 'http://127.0.0.1:8188/system_stats' -TimeoutSec 5 | Out-Null; $up = $true; break } catch { }
    }
    if (-not $up) {
        throw 'ComfyUI did not answer within 5 minutes. Look at the "LotVue360 AI Engine" window for the error (an old NVIDIA driver is the usual cause).'
    }
    # ComfyUI answers {} for a node it doesn't have; model lists are inside each loader's info.
    $node = (Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8188/object_info/WanFunInpaintToVideo' -TimeoutSec 30).Content
    if (-not $node.Contains('"WanFunInpaintToVideo"')) {
        throw 'This ComfyUI has no WanFunInpaintToVideo node. Delete the ComfyUI_windows_portable folder and run this setup again for the latest ComfyUI.'
    }
    foreach ($check in @(@{ Loader = 'UNETLoader'; Folder = 'diffusion_models' }, @{ Loader = 'CLIPLoader'; Folder = 'text_encoders' },
                         @{ Loader = 'VAELoader'; Folder = 'vae' })) {
        $listed = (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8188/object_info/$($check.Loader)" -TimeoutSec 30).Content
        foreach ($model in $Models | Where-Object { $_.Folder -eq $check.Folder }) {
            if (-not $listed.Contains($model.Name)) { throw "ComfyUI does not list $($model.Name). Close the AI engine window and run this setup again." }
        }
    }
    Write-Step 'AI engine ready. Restart LotVue360, open 3D Studio and pick "AI smooth".'
    Write-Host '    Keep the "LotVue360 AI Engine" window open while AI spins are made; each spin takes several minutes.'
}

if ($MyInvocation.InvocationName -ne '.') { Install-AISpin }
