# LotVue360 — LEDGER_LOG

> Dated, proof-only. Every line below is backed by a commit hash, an HTTP probe, a file count, or another number that was actually measured — no projections, no "should work." For the full narrative history and the numbered capability table, see `LEDGER.md` and `LOTVUE_CANONICAL_LEDGER.md` (kept on disk, not superseded by this file).

## 2026-06-12 — Origin
First commit `3a0c832`: "Initial commit - LotVue360 360 vehicle merchandising platform (GLO3D/Spyne/CarCutter alternative)." Verified via `git log --reverse` on 2026-09-03.

## 2026-08-10 — Baseline platform shipped
360 photo-spin viewer live (`/api/viewer`, `/vdp`, `/embed` — all 200). Real-3D Gaussian-splat reconstruction working on a real walkaround (COLMAP 150/150 frames solved, Brush `scene.ply` produced, VIN DSREAL01). 200 real vehicles scraped and imported from DriveSmart Auto Sales. CRM kanban live (`/crm`, 401 dealer-gated). Windows desktop installer built and cold-boot verified (311MB). Signed Android `.aab` produced (4.7MB, keystore on disk).

## 2026-08-11 — Generative-mesh tier killed
68 of 118 generated GLB meshes failed QA and were quarantined. Standard-tier generative mesh abandoned intentionally; do not resume. Real-3D splat retained as a premium, real-footage-only tier.

## 2026-08-12 — Studio layer, input gate, hotspots, feed, and a full end-to-end pass
`app/studio.py` shipped: background segmentation (rembg/u2net), alpha refine, contact shadow (tuned and pixel-verified — shadow pixels measured [70,73,77] against a [200,205,211] background, not eyeballed), studio compositing, optical-flow frame interpolation. `app/validate_video.py` shipped (316 lines): ORB-continuity gate live-tested — 422 rejection on a bad real-dealer photo set, 200 pass on a real 36-frame walkaround video; wired into both the upload route and the 3D-generation route, with a `force=1` override and UI-surfaced rejection reasons. Background asset pack (40 backdrops) rendered against a full-resolution real vehicle at 0.00% halo. Hotspot/damage-report layer shipped and DB-verified (`/vdp/{vin}/damage-report`, public 200, screenshot-inspected). Google Vehicle Listings feed shipped (`/api/feeds/google-vehicle-listings.xml`, public 200, validator 155/155). Full stranger-dealer 60-second test **PASSED** for every step except payment (fresh account → dashboard 200 → real walkaround upload/gate → 360 render → public VDP 200 → priced → Google feed validator 156/156).

## 2026-08-13 — Universal gallery-to-spin lane
`app/studio_spin.py` shipped: turns any dealer's existing gallery photos into a studio spin with zero manual curation (CLIP classification → whole-car detection → matte geometry → ORB frame ordering → composite). Proven clean on 3 real vehicles across different bodies/colors (Navigator, Accord, Outback).

## 2026-09-02 — Server down ~3 weeks, two live credentials confirmed dead, first real-dealer sample produced but not sent
Production server had been returning 502 since on/around 2026-08-12 because the Windows scheduled task that auto-starts it (`LOTVUE\StartApp`) was left Disabled — manually restarted this date (`/api/health` 200 local + public). Stripe secret key live-tested: `401 Expired API Key`. Gmail SMTP app password live-tested: `535 BadCredentials`. Pulled 42 real listing photos from a named live independent dealer (Select Auto, Dayton OH, selectautoohio.com) and produced a 7-angle studio spin on a real 2021 Camaro (stock 24218105); 4 of 7 angles judged clean on inspection. Status: sample produced, **not sent** to the dealer — sending requires Ahmad.

## 2026-09-03 — Docs reverse-engineered from disk evidence
Reverse-engineered README/PRD/LEDGER_LOG from disk evidence during empire inventory pass.

## 2026-10-04 — Current branch position and PRD gaps
Application source commit `08b9fcd` (`Secure walkaround intermediate artifacts`) is pushed on `arena/01a0ffa3-car-360-viewer`; docs-only workflow record `520c335` follows it. Local verification on the source commit: **189 tests passed in 4.68 s** (subprocess **5.73 s**, peak child RSS **141.8 MiB**); targeted walkaround/static/auth checks **19 passed in 1.39 s**; configured Ruff, Python and PyInstaller-spec compilation, `pip check`, `git diff --check`, and Node syntax checking passed. Walkaround ComfyUI paths are confined; assembly copies/staging are hidden; legacy public intermediate cleanup is fail-closed; hidden paths return HTTP 404; FFmpeg must succeed with nonempty regular output before atomic publication. No actual FFmpeg, ComfyUI/WAN/TTS generation, or media-quality review occurred.

GitHub CI run `37177998115` and desktop run `37177998121` for `08b9fcd` failed before any runner step because the check annotation explicitly said an Actions budget prevents further use; all current-source desktop build jobs were skipped. Docs-only runs `37178035732` and `37178035752` were blocked for the same reason. No current-source installer artifact exists. The last verified three-platform artifacts are from `7fa13aa`: CI `37140754753`, desktop matrix `37140754758`; artifact metadata listed Windows x64, Intel macOS x64, and Apple Silicon macOS arm64 archives, unexpired through 2026-11-02 UTC. Those artifacts predate the latest Dealer Studio and walkaround changes.

The August 12 stranger-dealer workflow and the August/September production, feed, credential, and visual checks remain historical, not current proof. PRD blockers remain: current hosted builds for the current source; a fresh end-to-end dealer run; owner-approved canonical price and live Stripe payment/receipt; current production/reboot verification; live lead-email delivery; full-resolution real-vehicle QA; measured plate blur (**synthetic only, not full pipeline**); background-model and data-use rights; plus owner-gated iOS/Play publishing and optional Theta hardware. See `LOTVUE_CANONICAL_LEDGER.md` for the ordered checklist.
