# Builds LotVue360-OneClick-windows-x64.exe on this PC (same steps as the Windows CI job).
# Run from the repository folder:  powershell -ExecutionPolicy Bypass -File Build-Windows-Installer.ps1
# Needs internet the first time (Python packages, FFmpeg, background-removal models ~355 MB).
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Step($text) { Write-Host "`n=== $text" -ForegroundColor Cyan }

Step 'Checking Python 3.11 (64-bit)'
$python = $null
try { $python = (& py -3.11 -c "import sys; print(sys.executable)" 2>$null) } catch {}
if (-not $python) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Python.Python.3.11 -e --accept-package-agreements --accept-source-agreements
        $python = (& py -3.11 -c "import sys; print(sys.executable)")
    }
}
if (-not $python) { throw 'Python 3.11 (64-bit) is required: https://www.python.org/downloads/release/python-3119/' }
Write-Host "Python: $python"

Step 'Checking Inno Setup 6'
$iscc = (Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    foreach ($candidate in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                             "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
        if (Test-Path -LiteralPath $candidate) { $iscc = $candidate; break }
    }
}
if (-not $iscc -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    winget install --id JRSoftware.InnoSetup -e --accept-package-agreements --accept-source-agreements
    foreach ($candidate in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
        if (Test-Path -LiteralPath $candidate) { $iscc = $candidate; break }
    }
}
if (-not $iscc) { throw 'Inno Setup 6 is required: https://jrsoftware.org/isdl.php' }
Write-Host "Inno Setup: $iscc"

Step 'Creating build environment (.venv-desktop)'
if (-not (Test-Path -LiteralPath '.venv-desktop\Scripts\python.exe')) { & py -3.11 -m venv .venv-desktop }
$venvPython = (Resolve-Path '.venv-desktop\Scripts\python.exe').Path
$env:PIP_CONSTRAINT = 'requirements-release-constraints.txt'
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements-desktop.txt
if ($LASTEXITCODE -ne 0) { throw 'Installing build dependencies failed' }
Remove-Item Env:PIP_CONSTRAINT

$buildId = 'local'
try { $buildId = (& git rev-parse --short HEAD 2>$null) } catch {}
if (-not $buildId) { $buildId = 'local' }

Step "Building the app bundle (build id $buildId)"
& $venvPython scripts/build_desktop.py --platform windows --output dist/desktop-release --build-id $buildId
if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed' }

Step 'Verifying the bundle (checksums, bundled models, notices)'
& $venvPython scripts/verify_desktop_artifact.py --archive dist/desktop-release/LotVue360-windows-x64.zip --platform windows --architecture x64 --build-id $buildId
if ($LASTEXITCODE -ne 0) { throw 'Bundle verification failed' }

Step 'Compiling the one-click installer'
& $iscc 'LotVue360.iss'
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
$installer = Get-Item -LiteralPath 'dist\desktop-release\LotVue360-OneClick-windows-x64.exe'
$hash = (Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText("$($installer.FullName).sha256", "$hash  $($installer.Name)`n", [Text.Encoding]::ASCII)

Write-Host "`nDONE: $($installer.FullName)" -ForegroundColor Green
Write-Host "SHA-256: $hash"
Write-Host "After installing, check with:  `"$env:LOCALAPPDATA\Programs\LotVue360\LotVue360.exe`" --self-test"
Write-Host "(the log in $env:LOCALAPPDATA\LotVue360\desktop_launch.log should end with 'background removal via isnet-general-use')"
