# Third-party notices for LotVue360

The repository's top-level `LICENSE` covers only LotVue360's first-party code. It does not replace the license or notice for any third-party component packaged by a release.

## FFmpeg and codecs

Each desktop archive includes the exact `FFmpeg`/`FFprobe` license output captured from the shipped binaries, the detected license text, build configuration, and binary SHA-256 values in `BUILD-MANIFEST.json`. Review `app/bin/FFMPEG-LICENSE.txt`, `app/bin/FFMPEG-BUILD-INFO.txt`, and `app/bin/LICENSE-FFMPEG-*` against the exact platform binaries. A binary may not be distributed by this build if it reports nonfree components or an unrecognized license.

The Linux x86_64 build is compiled locally from pinned FFmpeg `n8.0` commit `140fd653aed8cad774f991ba083e2d01e86420c7` and x264 commit `c24e06c2e184345ceb33eb20a15d1024d9fd3497`, with `--enable-gpl --enable-libx264`. It therefore reports **GPL-2.0-or-later**. Matching FFmpeg and x264 source archives are included under `app/bin/source/`; `BUILD-MANIFEST.json` records their SHA-256 values and `app/bin/FFMPEG-SOURCE-BUILD-INFO.json` records the pinned commits and build details. This source bundle and synthetic encoder smoke test are technical evidence only: they are not owner/legal approval for redistribution, commercial use, codec/patent terms, or the product's licensing model.

Windows and macOS use the platform binaries resolved through the desktop build's `static-ffmpeg` dependency. Their exact reported license and build configuration are recorded inside those archives; this packaging change does not assert that their source archives are included. Review those platform-specific binaries and source/offer obligations independently before redistribution.

The bundled CPython interpreter and standard library are licensed under the Python Software Foundation License Version 2; a copy is included as `app/bin/LICENSE-PYTHON-PSF-2.0.txt`. The frozen Python application also includes third-party packages declared by `requirements.txt` and `requirements-desktop.txt` (including FastAPI, Uvicorn, NumPy, OpenCV, Pillow, python-jose, Stripe SDK, ReportLab, qrcode, RemBG/ONNX Runtime and transitive dependencies). Their packaged `.dist-info/licenses` and notices should be reviewed against the manifest. The build manifest records the Python version. The release owner must review a version-specific dependency-license/SBOM report and applicable codec/patent/redistribution terms before publishing a customer-facing binary.

## Optional model and service terms

No RemBG model checkpoint is redistributed or selected by default. For example, BRIA RMBG-2.0's [official model card](https://huggingface.co/briaai/RMBG-2.0) lists a non-commercial model license; do not enable it for paid dealer work without a separate commercial agreement. Other checkpoints have separate license/provenance terms too. The local `LOTVUE_REMBG_LICENSE_ACCEPTED=1` setting is only an operator acknowledgment, not an automatic license check.

Optional ComfyUI, WAN, COLMAP/Brush, CUDA, provider SDKs, other model weights, and credentials are not redistributed by this desktop build. Their own terms still apply if an operator chooses to install and connect them.

This is a technical notice, not legal advice. Obtain owner/legal approval for any public/commercial release and verify notices against the exact binaries included. No product-owner approval or legal review is claimed.
