## Install

Download the **one-click installer** matching the computer and verify it against the adjacent `.sha256` sidecar before running it:

- **Windows x64:** `LotVue360-OneClick-windows-x64.exe` — double-click to install for the current user; no administrator rights are required.
- **macOS Intel:** `LotVue360-OneClick-macOS-x64.dmg` — open the disk image, then double-click `Install-LotVue360.command` inside.
- **macOS Apple Silicon:** `LotVue360-OneClick-macOS-arm64.dmg` — open the disk image, then double-click `Install-LotVue360.command` inside.
- **Linux x86_64:** `LotVue360-OneClick-linux-x64.run` — mark it executable if needed, then run it; no separate ZIP extraction or Python install is required.

The matching ZIP and `.zip.sha256` sidecar are also published as a portable/fallback package. Windows may show SmartScreen and macOS may show Gatekeeper warnings because the installers are unsigned; macOS builds are not notarized. Do not disable platform protections globally.

The first launch creates a separate writable per-user directory for the database, uploads, app settings, and local authentication secret. Uninstall keeps user data by default; back up that folder before removing data or moving computers. Never put `.env` secrets in the Git repository.

## Package scope and limitations

The desktop release contains the local Python app/runtime, core dependencies, FFmpeg/FFprobe, the generated 40-background catalog, and the CPU rembg/ONNX runtime. No pretrained checkpoint is selected or bundled; local background removal stays disabled until an operator verifies the exact model's commercial terms and provenance, then explicitly configures it. Optional ComfyUI/WAN models, CUDA/COLMAP/Gaussian reconstruction tools, hosted provider accounts, API keys, dealer approvals, and payment credentials are **not** in the package. Those integrations remain unconfigured until the owner supplies credentials and a connection check succeeds.

The Linux x86_64 FFmpeg build enables x264 and is GPL-2.0-or-later; matching FFmpeg/x264 source archives and hashes are included in the Linux ZIP and installer payload. This is technical source/provenance information, not a product-owner or legal approval to distribute. The owner must review the exact platform artifacts and version-specific dependency, codec/patent, license, and redistribution terms before a customer-facing release.

Native runner smoke tests install each package, run the local engine self-test, and verify uninstall/data retention. They are not owner acceptance on end-user hardware and do not establish production hosting, live billing, email delivery, external provider connections, or hosted dealer feeds.
