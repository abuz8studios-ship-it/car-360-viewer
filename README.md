# LotVue360

**LotVue360 is an automotive merchandising workbench for independent dealers.** This repository brings the existing capture, inventory, 360-photo, backdrop, public-viewer, CRM, and distribution routes together under an authenticated Dealer Studio. It is a working application codebase, but that does not by itself establish production service, real-vehicle image quality, paid-provider connectivity, or readiness to sell.

Built by Ahmad Odeh / ABUZ8 LLC (Dayton, Ohio). Product price remains **undecided and unapproved**.

## Current engineering and release status

**Historical hosted build evidence (2026-10-03, commit `7fa13aa`):** [CI run 37140754753](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37140754753) and [desktop matrix run 37140754758](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37140754758) passed for Windows x64 and Intel/Apple-Silicon macOS ZIPs, including native installer/runtime smoke tests. Those 30-day Actions artifacts do not represent the current source and are not current one-click installers. No tag-triggered GitHub Release has been published. A push to this session branch triggers the matrix; only a reviewed `v*` tag publishes a release.

**Historical packaging blocker (2026-10-04, branch commit `a919b7b`):** This change adds a per-user Linux x86_64 installer, archive verification, and a native-runner smoke test; it does **not** make the Windows/macOS target artifacts buildable on this Linux sandbox. The local build installed the desktop dependencies and staged the 40-background app payload, then stopped before PyInstaller because the static FFmpeg v8.0 asset is Git-LFS-backed and this environment cannot complete its GitHub media download (TLS verification/EOF). No ZIP, checksum, or current-source appliance was produced. The latest hosted runs—[CI 37180896436](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37180896436) and [Desktop releases 37180893727](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37180893727)—were blocked before runner steps; the annotation says GitHub's Actions budget is preventing further use, and desktop build jobs were skipped. Prior artifacts on `7fa13aa` do not contain the newer Dealer Studio changes. Treat the Linux installer code as unverified packaging glue until the native bundle is built and its frozen runtime smoke test passes.

**Local one-click packaging status (2026-10-09):** The current source produced a Linux x86_64 ZIP (430,996,462 bytes; SHA-256 `ed10c9e8d8826c5cdbdd04c74e1ae2622671df77e5265852d03fc63a9690dce3`) and self-extracting `.run` (369,652,689 bytes; SHA-256 `c7105c07d3ca32a0e46e280a10f0168f2e17b1fb1964f34cbb075bf8198f120b`). The Linux FFmpeg/x264 source build passed its synthetic H.264 encode/probe test; the archive verifier checked 8,017 ZIP entries and both pinned source-archive hashes. A native local smoke installed directly from the `.run` under paths containing spaces, ran the frozen app self-test, checked normal-uninstall data retention and explicit data deletion, and verified temporary extraction cleanup. The app payload is about 934 MiB before compression; the smoke test is not model-quality testing. This was a local-only build (`build_id=local-current`), using a temporary CPython shared-library workaround because the sandbox's system Python lacked the shared library PyInstaller requires. The local FFmpeg build used its C-only x264 fallback because neither NASM nor Yasm was available; CI attempts NASM installation and records the selected assembler. These ignored `dist/` files are not GitHub release artifacts. The current push triggered [Desktop releases run 37932726091](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37932726091), but GitHub rejected its verification job before any steps with “An Actions budget is preventing further use”; every native build job was skipped. Thus the current Windows/macOS one-click workflow still has no hosted native-runner build/smoke-test evidence. Linux FFmpeg is GPL-2.0-or-later. Product-owner/legal redistribution review remains outstanding.

**Historical source update (2026-10-03):** Dealer Studio now has a per-vehicle scene-render action that reuses `POST /api/photo/composite`, with an authenticated, owner-scoped image picker, explicit local/ComfyUI choice, scene selection, and a public-storage/manual-review warning. It also exposes the existing authenticated video-ad text-template route as a text-only script draft; it does not generate audio/video or publish anything. The current edit passed 176 local tests plus lint, compilation, dependency, diff, and inline-JavaScript syntax checks. No hosted build is claimed for these UI changes: the last successful three-platform artifacts are from `7fa13aa` and contain neither the scene action nor this script draft. The workflows triggered on current source commit `a35eeab` (CI run `37175397809` and desktop run `37175397877`) were blocked before runner steps because GitHub reported its Actions budget prevents further use. The preceding scene-action runs on `a2352c4` were blocked for the same reason, and earlier runs on `894bd84` were blocked by the account payment/spending-limit message.

**Previous source update (2026-10-04, commit `8f47bb2`):** Dealer Studio now also surfaces the existing authenticated walkaround start/status workflow behind static CUDA, explicit ComfyUI URL, local `factory.py`, FFmpeg, and vehicle-photo checks. Starting remains an explicit click; the page does not probe ComfyUI or establish WAN/TTS availability, voiceover, visual quality, or a successful render. Its output is under public vehicle storage and must be reviewed. Status polling distinguishes a queued job from an older saved video, and unexpected worker failures are surfaced with a generic error rather than leaked exception details. Current source passes 177 local tests in 3.23 s (3.90 s subprocess wall time; 143.8 MiB peak child RSS), plus configured lint, Python/spec compilation, dependency, diff, and inline-JavaScript checks. Route/job behavior is locally tested with generation stubbed, not run. Commit `8f47bb2` is pushed to the session branch; [CI run 37176348682](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37176348682) and [desktop run 37176348683](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37176348683) were blocked before runner steps because GitHub's Actions budget prevents further use. No current-source installer artifact is claimed.

**Current local follow-up (2026-10-04):** Walkaround source filenames returned by ComfyUI are confined to its configured output root; traversal and symlink paths are rejected. The engine's working copies of clips, voiceover, concat list, and FFmpeg staging output live in a hidden per-job temporary directory; ComfyUI's own source files remain in its configured output folder under that service's retention behavior. Legacy public `clip_*.mp4`, `voiceover.wav`, and `concat.txt` artifacts are removed before a new job (and failure to clean them stops the render). Storage-path middleware rejects dot-prefixed paths; local HTTP tests verify hidden intermediates return 404 while the final walkaround remains publicly served. FFmpeg must exit successfully and create a nonempty regular file before an atomic replace publishes it. Failed or empty-output rerenders report `error` and preserve any prior final video; silent-audio fallback also requires FFmpeg success and a nonempty output. Mocked regressions cover these paths; the full suite reports **189 passed in 4.68 s** (subprocess wall time **5.73 s**, peak child RSS **141.8 MiB**), and configured Ruff, Python compilation, dependency consistency, diff, and inline-JavaScript checks pass. No actual FFmpeg, ComfyUI, TTS, or walkaround media generation/quality test ran. Commit `08b9fcd` is pushed to the session branch. [CI run 37177998115](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37177998115) and [desktop run 37177998121](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37177998121) were blocked before runner steps because GitHub's Actions budget prevents further use; the desktop build jobs were skipped. No hosted tests or current-source installer artifact are available for this update.

**Previous local source update (2026-10-04, commit `0138b90`):** The existing walkaround engine scans at most 4,096 entries per candidate directory (worst case 8,192 across studio plus exterior fallback), keeps at most 72 JPEG candidates, and image-verifies no more than four selected angles under the configured photo-file/pixel limits and a cumulative validation cap of the smaller of 200 MiB or the configured photo batch. Dealer Studio shows these caps alongside the public-viewer and background-worker limits. The full suite reported **181 passed in 3.71 s** (4.55 s subprocess wall time; 142.4 MiB peak child RSS); configured lint, compilation, dependency, diff, and inline-JavaScript checks also passed. No walkaround generation ran. Commit `0138b90` was pushed to the session branch; [CI run 37177269414](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37177269414) and [desktop run 37177269305](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37177269305) were blocked before runner steps because the Actions budget prevents further use. There is no hosted verification or new installer artifact for that source update, and no live ComfyUI/TTS workflow was claimed.

The installers are per-user, keep writable data outside read-only bundle resources, include the Python runtime, declared core dependencies, FFmpeg/FFprobe, and the generated 40-background catalog. RemBG/ONNX runtime libraries are included, but **no background-removal checkpoint is selected or bundled by default**; operators must verify a model's commercial terms and explicitly enable it before weights are fetched. CUDA/ComfyUI/WAN model weights, COLMAP/Brush, paid service credentials, dealership/provider approvals, signatures, and notarization are not bundled.

For exact platform install steps and limitations, see [`docs/INSTALLATION.md`](docs/INSTALLATION.md). For evidence boundaries, resource caps, tests, and remaining owner-gated risks, see [`ENGINEERING_AUDIT_2026-10-03.md`](ENGINEERING_AUDIT_2026-10-03.md). Historical workstation notes remain dated evidence; see [`LEDGER_LOG.md`](LEDGER_LOG.md).

## Dealer Studio workflow

Dealer Studio is a deterministic control plane over the existing engines and routes; it does not autonomously publish or contact providers. After a dealer chooses a vehicle and confirms an action, it can bridge supported local work such as spin generation or Photo Studio preparation, show job state, open the buyer page, and report local subsystem readiness. The readiness panel is a **configuration-only snapshot**: “configured” is not a vendor login, live feed, email-delivery check, or quality sign-off.

The codebase includes routes and UI for:

- Dealer inventory and capture uploads with request, image-size, pixel, file-count, and worker limits.
- Photo processing, backdrop catalog, 360-frame/viewer routes, public vehicle pages, and interior/hotspot experiences. Dealer Studio now connects to `POST /api/photo/composite`: an authenticated vehicle owner selects a stored photo, scene, and explicit local or ComfyUI renderer. The picker returns at most 72 image references while scanning at most 4,096 entries across exterior/raw/source/studio folders; candidate validation is limited to 200 MiB cumulatively, with the existing 30 MiB/file and 40 MP/image guards. ComfyUI requires an owner-supplied API-format workflow; AI results are marked for review but remain on the publicly served `/storage` path, so the warning is advisory—not private staging or access control. The default local renderer is deterministic and rejects opaque photos when a transparent cut-out is required.
- CRM and lead workflows, import/export routes, and optional marketplace/feed connectors.
- Text-only listing/social voiceover-script drafts through the existing authenticated `/api/vehicle/{vin}/video-ad` template route. This is not video/audio synthesis, and no publication or messaging occurs; review vehicle details, price, and dealer name first.
- Optional walkaround video through the existing `/api/vehicle/{vin}/walkaround` background job. The Dealer Studio button is gated by static prerequisite checks and requires an explicit click. The generator scans at most 4,096 entries per source folder, keeps at most 72 JPEG candidates, and verifies up to four selected angles within the configured file/pixel caps and the smaller of a 200 MiB or configured photo-batch validation limit. It shares the bounded background pool (default two workers/four admitted jobs). ComfyUI output paths are confined; walkaround working copies of clips/audio, concat data, and staged video live in a hidden, cleaned per-job directory, and legacy public intermediate files are removed. A final MP4 is atomically published only after FFmpeg exits successfully and produces a nonempty regular file; failed rerenders leave the prior final intact and report an error. Dot-prefixed storage paths are rejected by the storage/auth gate. The readiness check does not contact ComfyUI or verify WAN/TTS nodes, weights, model quality, or successful rendering; TTS may fall back to silence, and the final generated video is publicly served.
- Optional video, background-removal, and Gaussian/COLMAP integrations whose engines, models, hardware, credentials, and real-world quality vary by installation.

Synthetic route tests verify representative behavior (including tenant isolation, background/catalog behavior, and public viewer output), not real-dealer image quality, full-pipeline performance, or production reliability. The plate-blur measurements in the audit are **synthetic only, not a full-pipeline benchmark**.

## Install a desktop build from GitHub

Download from **Actions → Desktop releases** only a successful run for the exact source commit you intend to install. The native matrix produces a one-click installer and a ZIP fallback for Windows x64, Intel macOS x64, Apple Silicon macOS arm64, and Linux x86_64; check that the run completed its platform installer/runtime smoke test before using an artifact. Older successful artifacts on `7fa13aa` do not contain the newer Dealer Studio changes and are not current-source releases.

Use the installer and matching `.sha256` sidecar:

- **Windows x64:** `LotVue360-OneClick-windows-x64.exe` — double-click to install per-user.
- **Intel Mac:** `LotVue360-OneClick-macOS-x64.dmg` — open the disk image, then double-click `Install-LotVue360.command`.
- **Apple Silicon Mac:** `LotVue360-OneClick-macOS-arm64.dmg` — open the disk image, then double-click `Install-LotVue360.command`.
- **Linux x86_64:** `LotVue360-OneClick-linux-x64.run` — mark executable if needed, then run; no prior ZIP extraction or Python install is required.

Verify the adjacent checksum before running an installer. SHA-256 detects corruption but does not authenticate the publisher. ZIP packages remain available as a portable/fallback install path; they require extraction before running their platform installer. Builds remain unsigned, and macOS disk images/apps remain unnotarized until the owner supplies signing credentials and approves the release.

The installer is a local desktop app, not a production-hosting appliance: first launch creates private per-user data, local authentication stays enabled, and provider credentials/models are not embedded. See [`docs/INSTALLATION.md`](docs/INSTALLATION.md) for platform instructions, data retention, bundled capabilities, and limits.

## Run from source

Requirements: Python 3.11+. From the repository root:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cd app
python -m uvicorn main:app --host 127.0.0.1 --port 8001
```

Source runs keep their default data beside the app. Set `LOTVUE_DATA_DIR` and optionally `LOTVUE_STORAGE_DIR` to use another writable location. Do not expose the development listener to a public network. The production-secret builder `app/build_env.py` is workstation-specific and is **never** part of desktop packaging.

Run local verification from the repository root:

```bash
python -m pytest -q
python -m ruff check app tests scripts
python -m compileall -q app tests scripts
```

## Ship an update

1. Merge the reviewed change to `main` and confirm the **CI** workflow is green on that commit.
2. Bump `MyAppVersion` in `LotVue360.iss` (and the `/api/health` version in `app/main.py`) to the new version.
3. Tag and push: `git tag -a v2.1.0 -m "LotVue360 v2.1.0" && git push origin v2.1.0`.

A `v*` tag runs **Desktop releases**: tests, the four native builds with installer smoke tests, then a GitHub Release with every installer, ZIP, and `.sha256` sidecar, using `.github/RELEASE_NOTES.md` as the notes. Running the same workflow manually (`workflow_dispatch`) builds 30-day artifacts without publishing. Both need GitHub Actions minutes on the owner's account.

## User data and connector security

The desktop app keeps its database, configuration, private JWT key, uploads, and logs in a per-user data directory separate from program files: `%LOCALAPPDATA%\LotVue360` on Windows and `~/Library/Application Support/LotVue360` on macOS. The first launch copies a blank template from `app/.env.example`. Use the installed system to create an account; choose a unique password and keep the data directory private.

Optional Google, social, marketplace, DMS, data-provider, email/SMS, local-model, and media-service connectors require the dealership owner's approved accounts, real credentials, service reachability, and appropriate legal rights. The blank template contains no live credentials. The local health panel does not probe external services. Configure secrets only in the local per-user `.env`; do not commit them, attach them to logs, or send them in chat/support tickets. This product does not provision third-party approvals or paid access.

Stripe remains disabled, public prices remain hidden, and no tier or amount is approved. The owner must select one canonical offer and separately verify any future live billing before enabling payments. The public Google feed likewise requires a real HTTPS dealership URL, complete dealer configuration, a secret token, and Google registration; a desktop-only URL is not a valid production feed.

## Resource defaults (from the current code audit)

- Video upload: **500 MiB**; individual photo: **30 MiB**; photo batch: **400 MiB**.
- Uploaded-image decode limit: **40 megapixels**; a single decoded RGB buffer at that size is already about **114 MiB**, before processing copies.
- Photo capture/Studio route limit: up to **72 photos**; background jobs default to **2 worker threads / 4 admitted jobs**.
- Public spin viewer: up to **120 frames**; public media-directory enumeration: up to **300 names**. The Dealer Studio scene picker returns at most **72 verified image references**, scans at most **4,096 directory entries**, and limits candidate validation to **200 MiB** per request (besides the existing **30 MiB/file / 40 MP** guards). These are application caps, not total disk quotas.
- Large/file-upload concurrency: **2 per process**; multi-process aggregate load requires separate sizing.
- ComfyUI scene generation: **1 active job per process**; additional concurrent requests receive `429 Retry-After: 30` rather than building an unbounded queue.

See the dated audit for measurement boundaries and details. The caps do not replace storage quotas, proxy timeouts, production memory limits, real-workload load tests, or an operations/retention policy.

## Historical documents and commercial status

Old workstation build sizes, historical account counts, former price sheets, production-outage notes, and prior capability checklists are not automatically current. Treat them as dated evidence and use the audit above for current verification. Pricing remains undecided. The tracked root `LICENSE` declares proprietary ABUZ8 LLC ownership and All Rights Reserved; the product owner should confirm customer-use and third-party redistribution terms before any public release.
