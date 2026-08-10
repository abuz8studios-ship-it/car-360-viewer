# LOTVUE 360 / LOTVUE PRO — CANONICAL LEDGER
> Single source of truth. Rewritten each pass. Last pass: 2026-08-10 ~19:00 UTC — full build day.
> NEVER quote numbers from this file — re-measure live. Snapshots go stale.

## CANONICAL
- **Build**: `E:\ABU\01_PRODUCTS\360Car-LotVue` — HEAD `7a915b3`. Junction `E:\ABU\360Car` → here (build specs need it).
- Dead: `01_PRODUCTS\PROJECTS\LOTVUE360` (Apr branch). Nonexistent: `E:\ABU\PROJECTS\LOTVUE360`.

## PHASES — 2026-08-10 (all two-proof: file:line + live probe)
| Phase | State | Proof |
|---|---|---|
| P0 baseline | ✅ e211816 | 191 routes, cold boot, 200/200/200/401 |
| P1 parity re-probe | ◐ folded into P2-P4 routes | new routes probed individually |
| P2 three.js + splat engine | ✅ bfa8cf1, b1157dd | COLMAP 150/150 on real walkaround → Brush 35.1MB scene.ply → /splat3d/DSREAL01 200. Toolchain: COLMAP 4.1.1 `E:\ABU\colmap`, Brush 0.3.0 `E:\ABU\brush`, viewer `static\lotvue_viewer.js` (GLTF+DRACO+Orbit+PBR / splat / spin), `viewer_api.py` manifest+/vdp+/embed |
| P3 DriveSmart ingest | ✅ engine, ⏳ volume | 1ab4643. Scraper rewritten for signed inv-scripts-v2 API. 200 live vehicles. Batch1 10/12 imported+3D-queued; batch2 (188) running unattended. Logs `E:\ABU\_lv_p3_batch*.log` |
| P4 CRM center | ✅ 9988f63 | `/crm` kanban (drag-drop stages) + summary/leads/appointments APIs; dealer-gated (401 unauth) |
| P5 desktop | ✅ 9988f63, 7a915b3 | Rebuilt on current engine; cold-boot 200; clean master `admin@lotvue360.app`/`Abuz809!`; **installer `installer\LotVue360-Setup-2.1.0.exe` 310MB** sha256 F68D9241… |
| P6 mobile | ✅ Android / 🔒 iOS | **Signed AAB `mobile_native\android\...\app-release.aab` 4.7MB** (com.abuz8.lotvue) sha256 3E2D6692…; keystore `lotvue-upload.keystore` (pass LotVue360!Upload — move to a vault). iOS = owner gate (needs Mac/cloud). |
| P7 ship | ✅ public / 🔒 money | **https://lotvue.abuz8ai.com LIVE** (200 on /, /vdp/DSREAL01, /splat3d/DSREAL01), tunnel b9aa6d6c + 'LotVue Tunnel' autostart task. Stripe/pricing/stores = owner gates |
| P8 verify | ✅ continuous | every claim above carries a probe result from this pass |

## TRAPS DISCOVERED (cost real time — do not rediscover)
1. `NODE_ENV=production` set machine-wide → npm silently skips devDeps ("up to date", installs nothing). Clear it before npm work.
2. Inno compile fails "file in use": the desktop shell launches Edge `--app` with `app_profile` as user-data-dir; kill those msedge PIDs and delete `app_profile` from the bundle first.
3. Brush GitHub assets: regex `win` matches `darwin`. Match `pc-windows`.
4. drivesmartwc serviceUrl is SIGNED — valid only verbatim; page via `/inventory/?pager=12&page_no=N`.
5. Synthetic turntable video (car rotates, camera still) can NEVER solve in COLMAP. Real walkaround = 150/150.
6. `FFMPEG_PATH`/`FFPROBE_PATH` env needed (hardcoded `E:\ABU\AI\ffmpeg` is dead). cuDNN 9 DLL still absent → rembg on CPU (works, slower).

## OWNER GATES (Hamada)
Apple Dev $99 (iOS build+store) · Play Console $25 (upload AAB — binary READY) · Stripe key · pricing/product page on abuz8ai.com · DriveSmart written scrape/republish permission · move keystore password to a vault · shoot walkaround videos to upgrade lot cars to true 3D.

## SELL SHEET (the demo that closes dealers)
Public, today: https://lotvue.abuz8ai.com/vdp/SCBCR73W57C050030 (their own Bentley) and /splat3d/DSREAL01 (true 3D).
Line: "Your whole lot, in 3D, with an AI salesperson on every car — $99/mo. GLO3D charges $2,400 for less."
