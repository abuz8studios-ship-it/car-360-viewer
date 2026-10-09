# LotVue360 desktop installation and connector setup

**Historical hosted verification (2026-10-03; commit `7fa13aa`):** [CI run 37140754753](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37140754753) and the [hosted matrix run 37140754758](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37140754758) passed on commit `7fa13aa`. The matrix built Windows x64, Intel macOS x64, and Apple Silicon macOS arm64 with the explicit ComfyUI scene bridge. Each ZIP's checksum, manifest, FFmpeg notices/binary hashes, and exclusions for user secrets and model weights were verified before native installer/runtime smoke tests and artifact upload. The three ZIPs and SHA-256 sidecars are in that run's **Artifacts** section with 30-day retention (through 2026-11-02 UTC). This earlier build does not include the current Dealer Studio scene-render UI action. CI run `37175397809` and desktop run `37175397877` for source commit `a35eeab` were blocked before runner steps because GitHub reported that the Actions budget prevents further use; this script-draft addition has local verification only. The preceding scene-action commit `a2352c4` was blocked for the same reason, and earlier attempts on `894bd84` were blocked by an account payment/spending-limit message. The optional ComfyUI workflow and model weights remain owner-supplied and are not packaged. No tag-triggered GitHub Release has been published. Builds are unsigned and macOS artifacts are not notarized; hosted runner smoke tests are not owner acceptance on end-user machines.

**Historical packaging blocker (2026-10-04, branch commit `a919b7b`):** Linux x86_64 install, archive-verification, and native smoke-test code has been added, but no current-source package is complete. The Linux build staged the app and 40 backgrounds, then stopped before PyInstaller because this sandbox could not download the Git-LFS-backed static FFmpeg 8.0 archive from GitHub's media host (TLS verification/EOF); no ZIP or checksum was produced. Current hosted runs [CI 37180896436](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37180896436) and [Desktop releases 37180893727](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37180893727) were blocked before runner steps by the Actions budget, and native Windows/macOS builds cannot run on this Linux host. The old `7fa13aa` artifacts therefore are not substitutes for current-source installers.

**Local one-click packaging status (2026-10-09):** A local Linux x86_64 build of current source produced `LotVue360-linux-x64.zip` (430,996,462 bytes; SHA-256 `ed10c9e8d8826c5cdbdd04c74e1ae2622671df77e5265852d03fc63a9690dce3`) and `LotVue360-OneClick-linux-x64.run` (369,652,689 bytes; SHA-256 `c7105c07d3ca32a0e46e280a10f0168f2e17b1fb1964f34cbb075bf8198f120b`). The Linux FFmpeg/x264 source build passed a synthetic H.264 encode/probe smoke test; the archive verifier checked 8,017 ZIP files, their manifest/binary hashes, and both pinned source-archive hashes. The native local smoke test verified the `.run` checksum, direct install under paths containing spaces, the frozen app self-test, data retention on normal uninstall, explicit user-data removal, and temporary extraction cleanup. This local artifact is not a GitHub Actions artifact or release. The sandbox required a temporary CPython shared-library workaround for PyInstaller; the local FFmpeg build used C-only x264 because NASM/Yasm were unavailable. The workflow attempts NASM installation and records assembler choice. The Linux payload is GPL-2.0-or-later; product-owner/legal redistribution approval remains outstanding. Current-source Windows/macOS installers still require successful hosted native builds and smoke tests.

## Get a desktop build

1. Open **Actions → Desktop releases** and choose a successful run for the exact source commit you intend to install. The native matrix targets Windows x64, Intel macOS x64, Apple Silicon macOS arm64, and Linux x86_64. Each platform job uploads a one-click installer, its SHA-256 sidecar, a portable ZIP, and the ZIP sidecar. Artifacts have 30-day retention. A formal GitHub Release is published only from a reviewed `v*` tag.
2. Download the one-click installer and adjacent `.sha256` sidecar matching the computer:
   - `LotVue360-OneClick-windows-x64.exe` for 64-bit Windows.
   - `LotVue360-OneClick-macOS-x64.dmg` for Intel Macs.
   - `LotVue360-OneClick-macOS-arm64.dmg` for Apple Silicon Macs.
   - `LotVue360-OneClick-linux-x64.run` for 64-bit x86 Linux.
3. Verify the installer before running it. In the download folder, Linux uses `sha256sum -c LotVue360-OneClick-linux-x64.run.sha256`; macOS uses `shasum -a 256 -c LotVue360-OneClick-macOS-x64.dmg.sha256` or the `arm64` filename. On Windows compare `(Get-FileHash .\LotVue360-OneClick-windows-x64.exe -Algorithm SHA256).Hash` with the first field in the `.exe.sha256` sidecar. A checksum verifies file integrity, not publisher identity or a digital signature.
4. Run the single-file installer for that platform; a separate Python install and ZIP extraction are not required. The ZIP remains available as a portable/fallback package.

### Windows

Double-click `LotVue360-OneClick-windows-x64.exe`. It installs per-user under `%LOCALAPPDATA%\Programs\LotVue360`, creates a Start Menu entry, and offers to start the local app. No administrator permission is needed. Uninstall from Windows **Settings → Apps**; program removal keeps the separate user-data folder by default. A Windows SmartScreen warning may appear because the executable is not Authenticode-signed.

The portable ZIP fallback contains `Install-LotVue360.cmd`, `Install-LotVue360.ps1`, and `Uninstall-LotVue360.ps1`. Extract the full ZIP before running those scripts. `Uninstall-LotVue360.ps1 -RemoveUserData` permanently removes the database, local secret, settings, and uploads as well as the app.

### macOS

Open the matching `.dmg`, then double-click `Install-LotVue360.command`. The app is installed for the current user at `~/Applications/LotVue360.app`, without administrator permission. The portable ZIP fallback contains the same command and `install-lotvue.sh`; extract it before running the installer. The installer intentionally does **not** remove macOS quarantine attributes.

The app and installer are not Developer ID-signed or notarized. If Gatekeeper blocks first launch, use Finder to right-click **LotVue360.app**, choose **Open**, and confirm the macOS dialog. If that dialog is unavailable, stop and obtain a signed build from the owner; do not disable Gatekeeper globally.

### Linux x86_64

Mark `LotVue360-OneClick-linux-x64.run` executable if the download did not preserve its mode, then run it (for example, `chmod +x ./LotVue360-OneClick-linux-x64.run && ./LotVue360-OneClick-linux-x64.run`). The self-extracting installer does not require a separate ZIP extraction, `sudo`, or Python install. It installs under `~/.local/opt/LotVue360`, adds a desktop-menu entry under `${XDG_DATA_HOME:-$HOME/.local/share}/applications`, and opens Dealer Studio in the system browser. Linux x86_64, a browser, and a usable graphical desktop session are required.

The ZIP fallback contains `Install-LotVue360.sh`; extract it and run the script from its top-level `LotVue360` folder. Both install paths keep writable user data outside the program files. Run `~/.local/opt/LotVue360/Uninstall-LotVue360.sh` to remove the app and menu entry. Uninstall keeps the database, settings, local secret, and uploads under `${XDG_DATA_HOME:-$HOME/.local/share}/LotVue360`. Data deletion is a separate destructive action: add `--remove-data` and confirm the prompt, or use `--remove-data --yes` only when you deliberately intend to delete it. If installed to a custom prefix, pass the same `--prefix DIR` to the uninstaller.

## What is bundled (and what is not)

The desktop build packages the Python 3.11 application runtime with the PSF license notice, declared core Python libraries, OpenCV/Pillow, CPU `rembg`/ONNX runtime libraries, FFmpeg/FFprobe, and the application's generated 40-background catalog. On Linux x86_64, FFmpeg is built from the pinned FFmpeg/x264 sources and the matching source archives are included; FFmpeg is GPL-2.0-or-later because x264 is enabled. See `THIRD_PARTY_NOTICES.md` and the exact build metadata inside each package. No product-owner license or redistribution approval is claimed. Its embedded self-test checks local health, the background API, the 8 compatibility-scene entries, background-model gate consistency, and that the health snapshot makes no external probes before GitHub uploads an artifact.

The `rembg`/ONNX Python runtime is included, but **no pretrained checkpoint is selected or bundled by default**. Background removal therefore stays disabled until the operator verifies the exact checkpoint's license and provenance, sets `LOTVUE_REMBG_MODEL` to its rembg model name, and sets `LOTVUE_REMBG_LICENSE_ACCEPTED=1` in the private `.env`. This acknowledgment is an operator guardrail, not legal verification. Only then may rembg download the weights on first inference, which needs network access. BRIA RMBG-2.0 has a non-commercial-only model license unless a separate commercial agreement is obtained; do not enable it for dealer work without that agreement. Other rembg checkpoints also require their own license/provenance review. No such review or model quality test is included in this build.

ComfyUI, WAN video-generation models, CUDA, COLMAP/Brush Gaussian reconstruction, Ollama, and other GPU/model-heavy tools are optional and not included. The build does not create third-party accounts, acquire paid services, or embed credentials. The desktop release contains no Stripe, SMTP, Twilio, marketplace, social, dealer-DMS, feed-registration, or provider API secrets.

## First launch and local data

The embedded app opens Dealer Studio and serves the web UI only on the local machine (`127.0.0.1`, default port `8360`). On first launch it creates a per-user data directory, copies a blank settings template, and keeps code and writable data separate:

| Platform | App code | Database, `.env`, JWT secret, uploads, local logs |
|---|---|---|
| Windows | `%LOCALAPPDATA%\Programs\LotVue360` | `%LOCALAPPDATA%\LotVue360` |
| macOS | `~/Applications/LotVue360.app` | `~/Library/Application Support/LotVue360` |
| Linux x86_64 | `~/.local/opt/LotVue360` | `${XDG_DATA_HOME:-$HOME/.local/share}/LotVue360` |
| Linux source run | repository app directory | `app/` by default; override with `LOTVUE_DATA_DIR` |

On Windows/macOS the first run makes `%LOCALAPPDATA%\LotVue360\.env` or `~/Library/Application Support/LotVue360/.env` from the bundled blank template. The app creates a private per-install JWT secret if one is not configured. Back up the complete user-data folder before uninstalling or changing computers. Do not copy that folder into GitHub issues, diagnostic bundles, or public storage.

The default mode keeps local authentication enabled, billing and pricing closed, and provider credentials blank. Use a unique account password. Do not expose the local HTTP listener to a public network; this installer is a local desktop tool, not a multi-dealer hosting deployment.

## Configure optional connectors securely

Edit the per-user `.env` using a plain-text editor only if you have the account owner’s permission and the required provider access. Restart the app after changes. Never commit that file or post its contents. The installed UI's **System & engine readiness** section is a configuration-only snapshot: it may report that fields or binaries are configured, but it does not sign in to vendors, test a token, register a dealer account, validate a live feed, or prove deliverability.

- **Google Vehicle Listings feed:** requires the dealer's public HTTPS URL, feed token, feed email, store/dealer identity, and address fields; Google account/feed registration and ingestion are separate steps. A local desktop URL is not a valid public feed origin.
- **Facebook/vehicle catalog and marketplace, eBay, Postiz, and FTP/DMS:** need the dealer's own integration approvals and credentials. A configured identifier is not a confirmed live account connection.
- **MarketCheck, Black Book, AutoCheck, Plate-to-VIN:** paid/licensed provider credentials must come from the account owner. Public NHTSA/EPA lookups still require outbound network access.
- **SMTP and Twilio:** require a working, approved relay/provider account; the blank template does not send email or SMS.
- **ComfyUI/Ollama and GPU engines:** install and secure those services separately, set URLs/model paths explicitly, and verify the local service's own health before enabling dependent work. No ComfyUI graph, custom nodes, or model weights are bundled.
- **Optional ComfyUI scene generation:** export an API-format prompt graph from the owner's ComfyUI installation and set `LOTVUE_COMFYUI_SCENE_WORKFLOW` to its path. The graph must place each of these literal string tokens exactly once in input values: `__LOTVUE_INPUT_IMAGE__`, `__LOTVUE_SCENE_PROMPT__`, and `__LOTVUE_OUTPUT_PREFIX__`; `__LOTVUE_NEGATIVE_PROMPT__` is optional and may appear once. Use the input token as the `LoadImage` filename, the prompt tokens in the graph's text inputs, and the output-prefix token in the output image node's filename prefix. The LotVue adapter stages the bounded image under `COMFYUI_INPUT`, submits the graph to `COMFYUI_URL`, requires exactly one saved output image, and removes its staged input/output afterward. Set `LOTVUE_COMFYUI_SCENE_TIMEOUT` (10–1800 seconds, default 300) for the local GPU. `POST /api/photo/composite` accepts `"engine":"comfyui"`; the authenticated Dealer Studio page now connects this existing route with an owner-scoped image picker, scene selector, and explicit local/ComfyUI renderer selector. The picker returns at most 72 verified image references while scanning at most 4,096 directory entries across the vehicle's exterior/raw/source/studio folders; candidate validation is limited to 200 MiB cumulatively and obeys the existing 30 MiB/file and 40 MP/image limits. The AI-generated result is marked for review, uses a `_comfyui` output suffix so it does not overwrite the deterministic result, and is not guaranteed to preserve vehicle details. The returned file is written under the existing publicly served `/storage` tree; `review_required` is an advisory response flag, not a private staging/access-control gate. The default `"local"` renderer is deterministic and requires a transparent vehicle cut-out; opaque original photos are rejected rather than covering the backdrop. For the shared cut-out-plus-composite path, use the existing `/api/vehicle/{vin}/background/process` workflow with an explicitly enabled, owner-approved removal model, or supply a transparent PNG cut-out yourself. AI mode fails closed if configuration or execution fails—there is no silent local fallback. Scene generation is limited to one active job per app process; additional concurrent requests receive HTTP 429 with a retry hint. This is a process-local cap, so multiple server workers multiply aggregate capacity. The ComfyUI service, graph, custom nodes, model weights, credentials, and workflow/model licenses remain owner-provided.
- **Dealer Studio video-script draft:** the page can call the existing `/api/vehicle/{vin}/video-ad` route to produce a local listing or social voiceover text template. It does not synthesize audio/video or publish/send the draft; verify the inventory fields, dealer name, and price before use.
- **Optional video walkaround:** Dealer Studio exposes the existing `/api/vehicle/{vin}/walkaround` job only when configuration-only checks find CUDA, explicit `COMFYUI_URL`, a `factory.py` under `LOTVUE_FACTORY_DIR`, FFmpeg, and vehicle photos. Generation starts only after an explicit click. The existing engine scans at most 4,096 entries per source folder, keeps at most 72 JPEG candidates, and verifies at most four selected angle images under the effective photo-file/pixel caps and a cumulative byte cap of `min(200 MiB, LOTVUE_MAX_PHOTO_BATCH_MB)`. It runs in the existing bounded worker pool (defaults: two workers and four admitted jobs). ComfyUI-returned paths are confined to its configured output folder; walkaround working copies of clips/audio/concat and FFmpeg staging files live in a hidden per-job directory, legacy public intermediates are cleaned, and dot-prefixed storage paths are rejected. The final MP4 is published atomically only after FFmpeg succeeds and creates a nonempty regular file; failed rerenders preserve the previous final and report an error. These are local resource and file-safety checks, not proof that ComfyUI, WAN/TTS nodes, weights, voiceover, or visual quality work; TTS may fall back to silence. The final generated video is served publicly under `/storage` and should be reviewed before sharing. Model weights, service, workflow, and licenses are owner-provided.
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

Build only on the target OS/architecture with Python 3.11–3.13, a shared-library interpreter supported by PyInstaller, and matching desktop wheels:

```bash
python -m pip install -c requirements-release-constraints.txt -r requirements-desktop.txt
python scripts/build_desktop.py --platform windows  # Windows x64
python scripts/build_desktop.py --platform macos    # macOS Intel or Apple Silicon host
python scripts/build_desktop.py --platform linux    # Linux x86_64
```

The script stages clean app source, generates the background catalog, stages FFmpeg/FFprobe, runs PyInstaller, and emits a checksummed ZIP. Linux x86_64 first builds FFmpeg n8.0 and x264 from the pinned source commits recorded above; run `python scripts/build_ffmpeg_linux.py --output build/ffmpeg/linux-x64`, then set `LOTVUE_FFMPEG_ROOT=build/ffmpeg/linux-x64` when running `python scripts/build_desktop.py --platform linux`. This path bundles the corresponding source archives and records their hashes. Because x264 enables GPL support, the resulting Linux FFmpeg is GPL-2.0-or-later; this is a technical build result, not owner/legal redistribution approval. Windows and macOS use their platform-specific `static-ffmpeg` binaries and must be built natively.

The Linux command also creates `LotVue360-OneClick-linux-x64.run`; Windows and macOS one-click installers are assembled in their native workflow jobs. The workflow verifies ZIP checksums/manifests, FFmpeg notices and Linux source-archive hashes, excluded secrets/models, and then installs and smoke-tests each platform installer before upload. The script never runs `app/build_env.py` and does not copy `.env`, JWT keys, database files, uploads, Android signing keys, or old workstation build outputs. Builds are unsigned; macOS outputs are not notarized.

## Owner actions still required

The hosted matrix has passed, but before a customer-facing `v*` release the owner should independently download and inspect the artifacts, review their manifests, FFmpeg notices, and checksums; generate/review a version-specific dependency-license or SBOM report; and test install/update/removal on representative end-user Windows, Intel/Apple-Silicon Mac, and Linux x86_64 computers. If signed public installers are intended, the owner must provide Apple Developer ID/Notary and Windows code-signing credentials through GitHub secrets; this workflow does not fabricate signatures or claim notarization. External provider credentials, account approvals, pricing decisions, and model-license clearance remain owner-provided.
