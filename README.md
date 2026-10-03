# LotVue360

**LotVue360 is an automotive merchandising workbench for independent dealers.** This repository brings the existing capture, inventory, 360-photo, backdrop, public-viewer, CRM, and distribution routes together under an authenticated Dealer Studio. It is a working application codebase, but that does not by itself establish production service, real-vehicle image quality, paid-provider connectivity, or readiness to sell.

Built by Ahmad Odeh / ABUZ8 LLC (Dayton, Ohio). Product price remains **undecided and unapproved**.

## Current engineering and release status

The cross-platform PyInstaller workflow has produced Windows x64, Intel macOS x64, and Apple Silicon macOS arm64 ZIP artifacts. The release code passed in [GitHub Actions run 37133573332](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37133573332): it checked each ZIP's checksum, manifest, FFmpeg/FFprobe hashes and notices, and secret/model exclusions, then passed native installer/runtime smoke tests and uploaded the artifacts. These are 30-day Actions artifacts; no tag-triggered GitHub Release has been published. A push to this session branch triggers the matrix, and only a `v*` tag publishes a release. The workflow can also be run manually after it is present on the default branch.

The installers are per-user, keep writable data outside read-only bundle resources, include the Python runtime, declared core dependencies, FFmpeg/FFprobe, and the generated 40-background catalog. RemBG/ONNX runtime libraries are included, but **no background-removal checkpoint is selected or bundled by default**; operators must verify a model's commercial terms and explicitly enable it before weights are fetched. CUDA/ComfyUI/WAN model weights, COLMAP/Brush, paid service credentials, dealership/provider approvals, signatures, and notarization are not bundled.

For exact platform install steps and limitations, see [`docs/INSTALLATION.md`](docs/INSTALLATION.md). For evidence boundaries, resource caps, tests, and remaining owner-gated risks, see [`ENGINEERING_AUDIT_2026-10-03.md`](ENGINEERING_AUDIT_2026-10-03.md). Historical workstation notes remain dated evidence; see [`LEDGER_LOG.md`](LEDGER_LOG.md).

## Dealer Studio workflow

Dealer Studio is a deterministic control plane over the existing engines and routes; it does not autonomously publish or contact providers. After a dealer chooses a vehicle and confirms an action, it can bridge supported local work such as spin generation or Photo Studio preparation, show job state, open the buyer page, and report local subsystem readiness. The readiness panel is a **configuration-only snapshot**: “configured” is not a vendor login, live feed, email-delivery check, or quality sign-off.

The codebase includes routes and UI for:

- Dealer inventory and capture uploads with request, image-size, pixel, file-count, and worker limits.
- Photo processing, backdrop catalog, 360-frame/viewer routes, public vehicle pages, and interior/hotspot experiences.
- CRM and lead workflows, import/export routes, and optional marketplace/feed connectors.
- Optional video, background-removal, and Gaussian/COLMAP integrations whose engines, models, hardware, credentials, and real-world quality vary by installation.

Synthetic route tests verify representative behavior (including tenant isolation, background/catalog behavior, and public viewer output), not real-dealer image quality, full-pipeline performance, or production reliability. The plate-blur measurements in the audit are **synthetic only, not a full-pipeline benchmark**.

## Install a desktop build from GitHub

1. Open **Actions → Desktop releases**, choose a successful run on this branch (a verified run is [37133573332](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37133573332)), and download the matching ZIP plus `.sha256` sidecar from its **Artifacts** section. No tag-triggered GitHub Release has been published yet.
2. Choose the artifact matching the computer:
   - `LotVue360-windows-x64.zip`
   - `LotVue360-macOS-x64.zip` (Intel)
   - `LotVue360-macOS-arm64.zip` (Apple Silicon)
3. Compare the ZIP with its adjacent `.sha256` file and extract the full archive.
4. Open the extracted top-level folder (`LotVue360` on Windows, `LotVue360-macOS-*` on macOS). Windows: double-click `Install-LotVue360.cmd`. macOS: double-click `Install-LotVue360.command` or run `bash install-lotvue.sh` from that folder.

The current artifacts are **unsigned**: no Windows Authenticode signature, Apple Developer ID signature, or notarization is claimed. The hosted runner smoke tests are not owner acceptance on end-user machines. Before a customer-facing release, the owner should independently inspect the artifacts, review the version-specific dependency/license inventory, and test installation/update/removal on supported Windows and macOS computers. Read [`docs/INSTALLATION.md`](docs/INSTALLATION.md) before installing.

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

## User data and connector security

The desktop app keeps its database, configuration, private JWT key, uploads, and logs in a per-user data directory separate from program files: `%LOCALAPPDATA%\LotVue360` on Windows and `~/Library/Application Support/LotVue360` on macOS. The first launch copies a blank template from `app/.env.example`. Use the installed system to create an account; choose a unique password and keep the data directory private.

Optional Google, social, marketplace, DMS, data-provider, email/SMS, local-model, and media-service connectors require the dealership owner's approved accounts, real credentials, service reachability, and appropriate legal rights. The blank template contains no live credentials. The local health panel does not probe external services. Configure secrets only in the local per-user `.env`; do not commit them, attach them to logs, or send them in chat/support tickets. This product does not provision third-party approvals or paid access.

Stripe remains disabled, public prices remain hidden, and no tier or amount is approved. The owner must select one canonical offer and separately verify any future live billing before enabling payments. The public Google feed likewise requires a real HTTPS dealership URL, complete dealer configuration, a secret token, and Google registration; a desktop-only URL is not a valid production feed.

## Resource defaults (from the current code audit)

- Video upload: **500 MiB**; individual photo: **30 MiB**; photo batch: **400 MiB**.
- Uploaded-image decode limit: **40 megapixels**; a single decoded RGB buffer at that size is already about **114 MiB**, before processing copies.
- Photo capture/Studio route limit: up to **72 photos**; background jobs default to **2 worker threads / 4 admitted jobs**.
- Public spin viewer: up to **120 frames**; public media-directory enumeration: up to **300 names**. These are application caps, not total disk quotas.
- Large/file-upload concurrency: **2 per process**; multi-process aggregate load requires separate sizing.

See the dated audit for measurement boundaries and details. The caps do not replace storage quotas, proxy timeouts, production memory limits, real-workload load tests, or an operations/retention policy.

## Historical documents and commercial status

Old workstation build sizes, historical account counts, former price sheets, production-outage notes, and prior capability checklists are not automatically current. Treat them as dated evidence and use the audit above for current verification. Pricing remains undecided. The tracked root `LICENSE` declares proprietary ABUZ8 LLC ownership and All Rights Reserved; the product owner should confirm customer-use and third-party redistribution terms before any public release.
