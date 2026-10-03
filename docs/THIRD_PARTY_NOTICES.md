# Third-party notices for LotVue360

The repository's top-level `LICENSE` covers only LotVue360's first-party code. It does not replace the license or notice for any third-party component packaged by a release.

Desktop archives include the exact `FFmpeg` and `FFprobe` license output and build configuration captured from the shipped binaries, a copy of the license text detected at build time, and an FFmpeg source/build reference. The release manifest records the detected FFmpeg license family and binary hashes. Check the archive's `app/bin/FFMPEG-LICENSE.txt`, `app/bin/FFMPEG-BUILD-INFO.txt`, and adjacent license text before redistribution; the binary's configure options may enable GPL components. The binary may not be distributed if it reports nonfree components or an unrecognized license.

The frozen Python application also includes third-party packages declared by `requirements.txt` and `requirements-desktop.txt` (including FastAPI, Uvicorn, NumPy, OpenCV, Pillow, python-jose, Stripe SDK, ReportLab, qrcode, RemBG/ONNX Runtime and their transitive dependencies). Each component keeps its own license and copyright. The build manifest records the Python version. The release owner must review a version-specific dependency-license/SBOM report and all applicable codec/patent/redistribution terms before publishing a customer-facing binary.

No RemBG model checkpoint is redistributed or selected by default. For example, BRIA RMBG-2.0's [official model card](https://huggingface.co/briaai/RMBG-2.0) lists a non-commercial model license; do not enable it for paid dealer work without a separate commercial agreement. Other checkpoints have separate license/provenance terms too. The local `LOTVUE_REMBG_LICENSE_ACCEPTED=1` setting is only an operator acknowledgment, not an automatic license check.

Optional ComfyUI, WAN, COLMAP/Brush, CUDA, provider SDKs, other model weights, and credentials are not redistributed by this desktop build. Their own terms still apply if an operator chooses to install and connect them.

This is a technical notice, not legal advice. Obtain owner/legal approval for any public/commercial release and verify notices against the exact binaries included.
