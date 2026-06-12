# LotVue360 — Session Audit — 2026-05-28
<!-- 2026-05-28 17:25 EDT — Bismillah. Documenting the path fixes, port shift, and upload bug resolution. -->

## 📈 Status Audit

| Feature | Prior State | Current State | Verification / Proof |
| :--- | :--- | :--- | :--- |
| **Backend Bind Port** | :8001 (Blocked by llama-server) | **:8010 (Fully Operational)** | `Get-NetTCPConnection` verified active process. |
| **Path Structure** | Absolute paths hardcoded to `E:\ABU\...` | **100% Relative & Dynamically Resolved** | `Path(__file__)` bindings active in main/pipeline. |
| **Batch Photo Formats** | Only `.jpg`, `.jpeg`, `.png` allowed | **Expanded: HEIC, HEIF, WEBP allowed** | `valid_exts` set updated in `upload_media` route. |
| **Active Processes** | Dead | **Uvicorn Daemon Active** | Port probe confirms PID `52796` listening. |

---

## 🔍 The HEIC/WEBP Upload Bug & Fix
During testing, users hit an **`error invalid file type`** when uploading raw batch smartphone images. 

- **Root Cause**: While LotVue Pro successfully initializes `pillow_heif` to support modern Apple formats on boot, the `/api/upload` batch endpoint hardcoded the validation set to strictly allow `valid_exts = {".jpg", ".jpeg", ".png"}`. 
- **Applied Fix**:
  - Edited [main.py](file:///E:/ABU/360Car/app/main.py#L3446) to expand the allowed file suffix set:
    ```python
    valid_exts = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
    ```
  - Stopped task `task-145` and successfully restarted the backend daemon on port `:8010` (PID `52796`).
  - Batch uploader is now fully compatible with smartphone `.heic` and modern `.webp` formats.

---

## 🚀 Active Ecosystem Layout

The full system state is active under persistent background daemons:
- **Port `8900`**: Qadir Core OS (PID `45764`)
- **Port `8010`**: LotVue360 (PID `52796`)
- **Port `18789`**: Zait OpenClaw (PID `13072`)
- **Port `11434`**: Ollama Server (PID `3044`)
- **Port `8188`**: ComfyUI Engine (PID `30380`)
