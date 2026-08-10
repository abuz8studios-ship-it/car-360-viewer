# LOTVUE PRO — CREATION ARTIFACT
> The chain to the answer: what was tried, measured, killed, survived. Append-on-pass.

## PASS 2026-08-10 — "find it, prove it, plan it" (Cowork / Opus)

### What was asked
Open the LotVue project, finish it to GLO3D spec, add the automotive AI 3D interactive CRM dealer center,
make it a desktop app + web app with sign-in + real three.js vehicle renders of drivesmartwc.com stock,
plus iOS and Android apps.

### Chain of evidence — how the canonical path was found

| Step | Method | Reliability | Result |
|---|---|---|---|
| 1 | memory `/areas/products-to-ship.md` said `E:\ABU\PROJECTS\LOTVUE360` | 0.40 memory | **KILLED** — `device_list_dir` → "does not exist" |
| 2 | Filename search `lotvue` across `E:\ABU` | 0.95 live probe | 14 hits, all `.md` docs in MISSION_CONTROL — no source tree |
| 3 | Listed `E:\ABU\01_PRODUCTS` | 0.95 live probe | Found `360Car-LotVue` **and** `PROJECTS\LOTVUE360` — two candidates |
| 4 | `git log` on both | **1.00 git** | `PROJECTS\LOTVUE360` = 1 commit, Apr 2026. `360Car-LotVue` = 8+ commits through 2026-07-06 |
| 5 | Cross-checked commit subjects against the GLO3D parity matrix | 1.00 git + 0.40 doc | Matrix names modules `a2_extensions/b_distribution/g_glo3d/mobile_app` and port 8360 → only `360Car-LotVue` has them |
| 6 | Imported `main.py` in a subprocess | 0.95 live probe | `IMPORT_OK routes= 191`, 8 modules self-registered |
| 7 | Booted it, HTTP-probed 4 paths | 0.95 live probe | 200 / 200 / 200 / 401 |

**Verdict: sahih.** Canonical = `E:\ABU\01_PRODUCTS\360Car-LotVue`. Three independent chains (git, import, HTTP).

### What was killed this pass
- The memory path `E:\ABU\PROJECTS\LOTVUE360` — does not exist. Corrected in memory the same turn.
- "LotVue is 70% / needs redeploy after a 502" — the 502 was **nothing listening**, not a broken app.
  The app boots clean and serves 200s. The number was measuring a dead tunnel, not the product.
- "LIVE — https://lotvue.abuz8ai.com" in the parity matrix header — stale as of this pass; no tunnel running.
- The `src-tauri` shell in `PROJECTS\LOTVUE360` — dead branch. Desktop path is PyInstaller + Inno Setup, which
  already produced a 40.59 MB working exe once.

### What survived and was strengthened
- FastAPI monolith + 8 self-registering modules — architecture is sound, boots in ~25s cold.
- `h_dealer_import.py` already naming `drivesmartwc` as a scrape target — the target dealer was already wired.
- Dual RTX 5090 with a deliberate split (cuda:0 = AI/background, cuda:1 = render). Real engineering, not a stub.
- 40 procedural backdrops rendered and on disk.
- PyInstaller specs + Inno Setup script — packaging was solved once; it only needs re-running.

### The measurement that changed the plan
`viewer3d.js`: **76 `THREE.` references, 0 GLTFLoader, 0 OrbitControls.** Everyone (including previous passes)
read "three.js viewer, 25 KB" and marked 3D done. It is a hand-rolled r128 scene with no model loader and no
orbit camera. That single grep moved "3D" from *done* to *the main build item*, and it is why P2 exists.

### Instruments left on disk
`E:\ABU\_lv_probe.ps1` · `_lv_probe2.ps1` · `_lv_boot.ps1` — re-runnable. Any future pass re-measures instead of
trusting this file.

### Next action (exactly one sentence)
Re-probe all 7 GLO3D parity-matrix product groups against the live `:8360` server and record an HTTP status code
per row, replacing every typed checkmark with a measurement.
