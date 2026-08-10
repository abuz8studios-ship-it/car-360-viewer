# LOTVUE 360 / LOTVUE PRO — CANONICAL LEDGER
> The single source of truth for this product. Rewritten each pass, never appended.
> Last pass: 2026-08-10 14:2x UTC — Qadir'A (Cowork/Opus)

---

## 0. CANONICAL PATH (memory was wrong — corrected this pass)

| | |
|---|---|
| **CANONICAL BUILD** | `E:\ABU\01_PRODUCTS\360Car-LotVue` |
| Git HEAD | `cbe6022` — 2026-07-06 18:02 EDT — "360 TRUE-SPIN ENGINE: min-4-photo CarCutter-parity 360 + WAN generative arcs + guided capture" |
| Entry point | `app\main.py` (399,119 chars) → `uvicorn.run(app, host=0.0.0.0, port=PORT)` L8328 |
| **DEAD / DO NOT EDIT** | `E:\ABU\01_PRODUCTS\PROJECTS\LOTVUE360` — single commit `00ffff0` "Initial LotVue360 SaaS deployment", Apr 2026, Cloudflare-Worker era. Has an unused `src-tauri` shell. |
| **STALE MEMORY LINE KILLED** | `E:\ABU\PROJECTS\LOTVUE360` — this path does not exist on disk. |

---

## 1. MEASURED STATE (live probe 2026-08-10, not quoted from any baseline file)

**Cold boot: PASS.** `python main.py` → PID bound `:8360`, "Application startup complete."

| Probe | Result |
|---|---|
| `GET /` | **200** — 35,678 bytes |
| `GET /mobile` | **200** — 27,319 bytes (installable PWA) |
| `GET /docs` | **200** — OpenAPI |
| `GET /api/vehicles` | **401** — auth_gate enforcing (correct behaviour) |
| `len(app.routes)` | **191** |
| GPU | 2× NVIDIA RTX 5090, 31.8 GB each, CUDA true. AI/bg = cuda:0, render = cuda:1 |
| Backgrounds | 40 system backdrops ready |
| Python / Node / npm | 3.11.9 / v25.8.1 / 11.11.0 |

**Modules that self-register at boot (stdout-confirmed, 8/8):**
`a2_extensions` (13 routes) · `b_distribution` (17) · `c_intelligence` (10) · `f_control` (5) ·
`g_glo3d` (11) · `h_dealer_import` (4) · `mobile_app` (3) · `auth_gate` (21 exact + 15 prefix public paths)

**Heavy engines present:** `gaussian_3d.py` (40 KB, splatting) · `commercial_pipeline.py` (37 KB, WAN 2.2 + Kokoro TTS) · `wan_comfy_api.py` (17 KB) · `h_dealer_import.py` (34 KB, **already targets drivesmartwc**)

---

## 2. THE THREE REAL GAPS (everything else is already built)

### GAP A — the 3D is not actually three.js-grade
Measured in `app\static\`:

| File | three refs | GLTFLoader | OrbitControls | Verdict |
|---|---|---|---|---|
| `viewer3d.js` (25 KB) | 76 | **0** | **0** | three r128 off cdnjs, no model loader, no orbit camera |
| `splat_viewer.html` (8 KB) | 1 | 0 | 0 | three 0.164.1 ESM — Gaussian splat only |
| `lotvue_3d_viewer.js/html` | 0 | 0 | 0 | not three.js at all |

Today's "3D" is photo-interpolated spin + Gaussian splats. **There is no GLB/PBR/OrbitControls vehicle renderer.** That is exactly the thing the mission names.

### GAP B — desktop binary is behind the engine
`dist\LotVue360_standalone\LotVue360.exe` = 40.59 MB, **2026-06-12**.
HEAD engine commit = **2026-07-06**. The shipped exe predates the TRUE-SPIN engine by 24 days. PyInstaller specs exist (`LotVue360.spec`, `_standalone.spec`, `_dbg.spec`) + Inno Setup `LotVue360.iss`. Rebuild, don't rewrite.

### GAP C — mobile is a PWA, not store binaries
`/mobile` serves an installable PWA (12-angle guided capture, client-side ZXing VIN scan, live upload, `manifest.json` + `service-worker.js` present). **No iOS archive, no Android AAB, nothing submitted.** Capacitor wrap is the shortest correct path — same codebase, real native camera/push/deep-links, real store artifacts.

---

## 3. TARGET — drivesmartwc.com (measured 2026-08-10)

Drive Smart Auto Sales, two Ohio lots (West Chester + Fairfield).
Stack: **WordPress + DealerCenter**, Slider Revolution 6.7.14, **media on Azure Blob Storage**.
Inventory routes: `/inventory/`, `/west-chester-inventory/`, `/fairfield-inventory/`, plus `/car-finder/`, `/schedule-a-test-drive/`.
`h_dealer_import.py` already names `drivesmartwc` as a scrape target → scrape + photo download + 3D pipeline hand-off exists. **Not yet run to completion against the live lot.**

---

## 4. THE PLAN — P1 → P8 (each phase closes with two-proof evidence)

| # | Phase | Definition of done |
|---|---|---|
| P0 | Verified baseline | ✅ **DONE 2026-08-10** — canonical path fixed, 191 routes, boots clean |
| P1 | GLO3D parity closure | Every row of the 7-product parity matrix re-probed against live `:8360` and carrying an HTTP status code. FAILs closed. |
| P2 | Real three.js renderer | GLTFLoader + OrbitControls + PBR + HDRI env + hotspot layer + paint-swap + interior toggle, wired to the existing spin/splat pipeline |
| P3 | DriveSmart ingest → 3D | Real West Chester + Fairfield inventory scraped, photos pulled, 3D VDPs live for actual stock |
| P4 | Dealer CRM center | Lead inbox, vehicle-linked AI BDC threads, appointment calendar, pipeline stages, dealer/user roles on `auth_gate` |
| P5 | Desktop rebuild | Packaged exe cold-boots on current engine, installer built, sign-in reaches the web tier |
| P6 | Native iOS + Android | Capacitor wrap → Android AAB + iOS archive, native camera/push/deep-link |
| P7 | Ship | `lotvue.abuz8ai.com` tunnel restored, Stripe live, pricing + product page, GitHub release + SHA256, 2-min demo |
| P8 | Verification | Static `file:line` + live probe for every claim; ledger, SHA256, git, memory updated |

---

## 5. OWNER GATES (Hamada only — I cannot clear these)

1. **Apple Developer Program** — $99/yr. Blocks the iOS build being signed or submitted. Nothing else in P6 is blocked.
2. **Google Play Console** — $25 one-time. Blocks Android submission (the AAB itself can be built without it).
3. **Stripe key** — expired. Blocks P7 checkout, blocks nothing upstream.
4. **Cloudflare tunnel** — `lotvue.abuz8ai.com` currently resolves to nothing served; needs `cloudflared` running against `:8360`.
5. **DriveSmart permission** — written OK from the dealer to scrape and republish their inventory photos before this goes public.

---

## 6. STANDING RULES FOR THIS PRODUCT

- Never quote a route count, version, or score from this file — **re-measure it live**. This ledger is a snapshot, and snapshots go stale.
- The build that RUNS is not always the build that gets EDITED. Right now they are the same tree; keep it that way.
- Port **8360** is LotVue. `:8001` is the Nemo brain. Do not cross them.
- Every pass updates: this ledger → SHA256 manifest → git commit → memory `/areas/products-to-ship.md`.
