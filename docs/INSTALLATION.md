# LotVue360 desktop installation and connector setup

**Status as of 2026-10-03:** the cross-platform packaging and GitHub Actions workflow are implemented in this branch. They have not yet produced hosted Windows/macOS artifacts, and this unsigned build has not been run on a Windows or Mac host from this workspace. A GitHub release exists only after a `v*` tag triggers the workflow and its per-platform smoke tests pass. Do not treat the workflow definition as proof that an artifact or hosted service is already live.

## Get a Windows or Mac build from GitHub

1. A push to the session branch starts the Windows/macOS build and installer-smoke workflow. After this workflow is merged to the default branch, a maintainer can also select **Actions → Desktop releases → Run workflow**. Download a published `v*` release only after its matrix jobs succeed.
2. Download the ZIP matching the computer:
   - `LotVue360-windows-x64.zip` for 64-bit Windows.
   - `LotVue360-macOS-x64.zip` for Intel Macs.
   - `LotVue360-macOS-arm64.zip` for Apple Silicon Macs.
3. Download its adjacent `.zip.sha256` file and verify the checksum before running the installer. A checksum verifies file integrity, not publisher identity or a digital signature.
4. Extract the complete ZIP and open its top-level folder (`LotVue360` on Windows; `LotVue360-macOS-x64` or `LotVue360-macOS-arm64` on Mac).

### Windows

From that extracted folder, double-click `Install-LotVue360.cmd`, or run `Install-LotVue360.ps1`. Installation is per-user under `%LOCALAPPDATA%\Programs\LotVue360`; it does not need administrator permission. It creates Start Menu links and starts the local app. Add `-DesktopShortcut` to the PowerShell command only if you also want a desktop shortcut. If updating, exit a running LotVue360 instance first. Use the included uninstaller; by default it keeps the separate user data. `Uninstall-LotVue360.ps1 -RemoveUserData` permanently removes the database, local secret, settings, and uploads as well as the app.

Windows may display a SmartScreen warning because these builds are not Authenticode-signed.

### macOS

Double-click `Install-LotVue360.command` or run `bash install-lotvue.sh`. By default, the app is installed for the current user in `~/Applications/LotVue360.app`; no administrator permission is needed. `bash install-lotvue.sh --no-open` installs without opening it. `--applications` targets `/Applications` and may require an administrator account. The installer intentionally does **not** remove macOS quarantine attributes.

The app is not Developer ID-signed or notarized. If Gatekeeper blocks it, use Finder to right-click **LotVue360.app**, choose **Open**, and confirm the dialog. If the dialog is not available, stop and obtain a signed build from the owner; do not disable Gatekeeper globally.

## What is bundled (and what is not)

The desktop build packages the Python 3.11 application runtime and declared core Python libraries, OpenCV/Pillow, CPU `rembg`/ONNX runtime libraries, FFmpeg/FFprobe, and the application's generated 40-background catalog. Its embedded self-test checks local health, the background API, the 8 compatibility-scene entries, background-model gate consistency, and that the health snapshot makes no external probes before GitHub uploads an artifact.

The `rembg`/ONNX Python runtime is included, but **no pretrained checkpoint is selected or bundled by default**. Background removal therefore stays disabled until the operator verifies the exact checkpoint's license and provenance, sets `LOTVUE_REMBG_MODEL` to its rembg model name, and sets `LOTVUE_REMBG_LICENSE_ACCEPTED=1` in the private `.env`. This acknowledgment is an operator guardrail, not legal verification. Only then may rembg download the weights on first inference, which needs network access. BRIA RMBG-2.0 has a non-commercial-only model license unless a separate commercial agreement is obtained; do not enable it for dealer work without that agreement. Other rembg checkpoints also require their own license/provenance review. No such review or model quality test is included in this build.

ComfyUI, WAN video-generation models, CUDA, COLMAP/Brush Gaussian reconstruction, Ollama, and other GPU/model-heavy tools are optional and not included. The build does not create third-party accounts, acquire paid services, or embed credentials. The desktop release contains no Stripe, SMTP, Twilio, marketplace, social, dealer-DMS, feed-registration, or provider API secrets.

## First launch and local data

The embedded app opens Dealer Studio and serves the web UI only on the local machine (`127.0.0.1`, default port `8360`). On first launch it creates a per-user data directory, copies a blank settings template, and keeps code and writable data separate:

| Platform | App code | Database, `.env`, JWT secret, uploads, local logs |
|---|---|---|
| Windows | `%LOCALAPPDATA%\Programs\LotVue360` | `%LOCALAPPDATA%\LotVue360` |
| macOS | `~/Applications/LotVue360.app` | `~/Library/Application Support/LotVue360` |
| Linux source/frozen fallback | bundle/source directory | `${XDG_DATA_HOME:-~/.local/share}/LotVue360` for frozen builds |

On Windows/macOS the first run makes `%LOCALAPPDATA%\LotVue360\.env` or `~/Library/Application Support/LotVue360/.env` from the bundled blank template. The app creates a private per-install JWT secret if one is not configured. Back up the complete user-data folder before uninstalling or changing computers. Do not copy that folder into GitHub issues, diagnostic bundles, or public storage.

The default mode keeps local authentication enabled, billing and pricing closed, and provider credentials blank. Use a unique account password. Do not expose the local HTTP listener to a public network; this installer is a local desktop tool, not a multi-dealer hosting deployment.

## Configure optional connectors securely

Edit the per-user `.env` using a plain-text editor only if you have the account owner’s permission and the required provider access. Restart the app after changes. Never commit that file or post its contents. The installed UI's **System & engine readiness** section is a configuration-only snapshot: it may report that fields or binaries are configured, but it does not sign in to vendors, test a token, register a dealer account, validate a live feed, or prove deliverability.

- **Google Vehicle Listings feed:** requires the dealer's public HTTPS URL, feed token, feed email, store/dealer identity, and address fields; Google account/feed registration and ingestion are separate steps. A local desktop URL is not a valid public feed origin.
- **Facebook/vehicle catalog and marketplace, eBay, Postiz, and FTP/DMS:** need the dealer's own integration approvals and credentials. A configured identifier is not a confirmed live account connection.
- **MarketCheck, Black Book, AutoCheck, Plate-to-VIN:** paid/licensed provider credentials must come from the account owner. Public NHTSA/EPA lookups still require outbound network access.
- **SMTP and Twilio:** require a working, approved relay/provider account; the blank template does not send email or SMS.
- **ComfyUI/Ollama and GPU engines:** install and secure those services separately, set URLs/model paths explicitly, and verify the local service's own health before enabling dependent work.
- **Stripe:** both feature and approval gates stay off. No price has been selected; do not enable billing until the owner approves one canonical offer and performs controlled live test transactions.

The readiness page is available at `/studio`; its non-sensitive local configuration snapshot is `GET /api/system/health`. That endpoint deliberately performs no external probes. A provider-specific test is still required before describing any integration as connected.

## Run from source (developers)

Use Python 3.11 and install from the repository root:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cd app
python -m uvicorn main:app --host 127.0.0.1 --port 8001
```

The source-run data default remains `app/`; `LOTVUE_DATA_DIR` and `LOTVUE_STORAGE_DIR` can point at other writable directories. For tests and lint, run from the repository root:

```bash
python -m pytest -q
python -m ruff check app tests scripts
```

## Rebuild locally

Build only on the target OS/architecture with Python 3.11–3.13 and the matching desktop wheels:

```bash
python -m pip install -c requirements-release-constraints.txt -r requirements-desktop.txt
python scripts/build_desktop.py --platform windows  # Windows x64
python scripts/build_desktop.py --platform macos    # macOS Intel or Apple Silicon host
```

The script stages app source into a temporary clean directory, generates background assets there, obtains static FFmpeg/FFprobe for the current OS, runs PyInstaller, and emits a ZIP plus SHA-256 checksum under `dist/desktop-release/`. GitHub Actions installs the CPython 3.11 package constraints in `requirements-release-constraints.txt`, then runs the frozen launcher self-test before uploading the artifact. Platform-only runner dependencies may resolve separately. The script never runs `app/build_env.py` and does not copy `.env`, JWT keys, database files, uploads, Android signing keys, or old workstation build outputs. Building locally is not a signature or notarization step.

## Owner actions still required

A repository maintainer must run the GitHub workflow (or push a reviewed `v*` tag), review successful artifacts and SHA-256 files, and—if public release is intended—supply Apple Developer ID/Notary credentials and Windows code-signing credentials through GitHub secrets. The current workflow intentionally does not fabricate signatures or claim notarization. Owners must separately test install/update/uninstall on actual supported Windows and Intel/Apple-Silicon Mac computers, review legal redistribution terms for all bundled dependencies and FFmpeg binaries, and provide any external provider credentials.
