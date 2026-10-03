## Install

- **Windows x64:** Download and extract `LotVue360-windows-x64.zip`, open the extracted `LotVue360` folder, then double-click `Install-LotVue360.cmd` (or run `Install-LotVue360.ps1`). The installer is per-user and does not require administrator rights.
- **macOS Intel:** Download and extract `LotVue360-macOS-x64.zip`; open the extracted `LotVue360-macOS-x64` folder and run `Install-LotVue360.command` or `bash install-lotvue.sh`.
- **macOS Apple Silicon:** Download and extract `LotVue360-macOS-arm64.zip`; open the extracted `LotVue360-macOS-arm64` folder and run `Install-LotVue360.command` or `bash install-lotvue.sh`.
- Compare the downloaded archive with its adjacent `.sha256` file before running it.

The first launch creates a separate writable user-data directory for the database, uploads, app settings, and local authentication secret. Do not put `.env` secrets in the Git repository.

## Package scope and limitations

The desktop release contains the local Python app/runtime, core dependencies, FFmpeg/FFprobe, the generated 40-background catalog, and the CPU rembg/ONNX runtime. No pretrained checkpoint is selected or bundled; local background removal stays disabled until an operator verifies the exact model's commercial terms and provenance, then explicitly configures it. Optional ComfyUI/WAN models, CUDA/COLMAP/Gaussian reconstruction tools, hosted provider accounts, API keys, dealer approvals, and payment credentials are **not** in the package. Those integrations remain unconfigured until the owner supplies credentials and a connection check succeeds.

These application bundles are **not Developer ID signed or notarized**. They are GitHub Actions build artifacts; Windows SmartScreen or macOS Gatekeeper may show an unsigned-app warning. No live billing, email delivery, external provider connection, or hosted dealer feed is implied by the build.
