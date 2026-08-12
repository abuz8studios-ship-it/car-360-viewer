# LOTVUE 360 / LOTVUE PRO — CANONICAL LEDGER
> Single source of truth. REWRITTEN each pass, never appended. Last pass: 2026-08-12 (Cowork: P0 gate + P1a/P1b/P1c shipped).
> NEVER quote numbers from this file — re-measure live. Snapshots go stale.

## CANONICAL
- **Build**: E:\ABU\01_PRODUCTS\360Car-LotVue — junction E:\ABU\360Car → here.
- Dead: 01_PRODUCTS\PROJECTS\LOTVUE360 (Apr branch). Nonexistent: E:\ABU\PROJECTS\LOTVUE360.
- Live: https://lotvue.abuz8ai.com
- Companion artifacts (same folder): LOTVUE_CREATION_ARTIFACT.md, LOTVUE_GOAL_ARTIFACT.md, COWORK_PROMPT.md

## CAPABILITY LEDGER (DONE rows carry date + commit/probe; re-verify before building on any row)
| # | Capability | Status | Evidence | Owner gate |
|---|---|---|---|---|
| 1 | Cold boot | DONE 08-12 | watchdog relaunches uvicorn :8360 after kill; / 200 local+public | no |
| 2 | Photo-spin viewer 3-tier | DONE 08-10 bfa8cf1 | /api/viewer, /vdp, /embed all 200 (re-probed 08-12) | no |
| 3 | Real-3D splat (premium) | DONE 08-10 b1157dd | DSREAL01 live, COLMAP 150/150, Brush scene.ply | no |
| 4 | DriveSmart scraper+import | DONE 08-10 1ab4643 | 200 vehicles, batches imported | no |
| 5 | CRM kanban | DONE 08-10 9988f63/7a915b3 | /crm 401 dealer-gated | no |
| 6 | Desktop installer | DONE 08-10 | LotVue360-Setup 310MB, cold-boot PASS | no |
| 7 | Android AAB | DONE 08-10 | signed 4.7MB, keystore on disk | Play $25 unpaid |
| 8 | iOS app | BLOCKED | — | YES: Mac/cloud build |
| 9 | AI bg replacement | DONE 08-12 | app/studio.py, --probe pixel-verified shadow | no |
| 10 | Generative mesh (standard) | KILLED 08-11 | 68/118 blob GLBs quarantined. DO NOT RESUME | intentional |
| 11 | Quality gate validate_video.py | DONE 08-12 | 316 lines; ORB-continuity is the discriminator (aspect-spread + bg-motion measured NON-discriminating: DS8097 spread 0.822, bg 0.154). CLI: DSREAL01 raw PASS / DS8097 FAIL 4/8 weak pairs. Live: /api/upload 422 capture_rejected (DS8097 photos) + 200 gate PASS (real walkaround video, 36 frames). Wired both branches, force=1 override | no |
| 12 | Bg asset pack | DONE 08-12 | Disk truth: 40 bgs 1920x1080 in static/backgrounds; studio.py was reading EMPTY static/studio_bg (gradient fallback) — load_bg now falls back to full library. All 40 rendered vs WDDEJ7EB9CA029569 full-res: halo 0.00% (metric injection-validated) + visual pass. NOTE: gradient backdrops, not photo scenes — photoreal pack = upgrade path | no |
| 13 | Hotspot/damage layer | DONE 08-12 | DB migration close_up; POST/GET carry condition_type/severity/note/close_up (400 without severity); viewer manifest merges DB rows (frame-resolved); /vdp/{vin}/damage-report printable, public 200, headless-Edge screenshot inspected | no |
| 14 | Google Vehicle Listings feed | DONE 08-12 | /api/feeds/google-vehicle-listings.xml public 200; rebuilt to published spec (fetched 08-12): +g:vin +dealership_name/address +trim, brand→make, color→exterior_color, XML-escaped, unsellable rows skipped; validator 155/155 items pass required attrs | no |
| 15 | FB Marketplace auto-post | RESEARCHED 08-12 | Marketplace dealer API/feeds DISCONTINUED 2023-01-30; only personal-account assisted posting (~10/day/acct). MVP live: /api/marketplace/facebook/{vin}/pack 200 (guardrailed listing pack) + catalog.csv for Meta vehicle ADS. No partner program to apply to | resolved: no API exists |
| 16 | Ricoh Theta interior | NOT STARTED | — | YES: hardware |
| 17 | Stripe checkout | NOT DONE | key expired | YES: refresh key |
| 18 | Pricing page abuz8ai.com | NOT DONE | needs #17 | no |
| 19 | DriveSmart written permission | NOT DONE | — | YES: owner obtains |

## OWNER GATES (Cowork cannot do these)
- iOS build (Mac or cloud build service)
- Apple Dev $99/yr + Google Play $25
- Stripe API key refresh (blocks #17→#18 pricing/checkout, the last big 60-second-test gap)
- DriveSmart written permission for scraped inventory as GTM dataset
- Ricoh Theta hardware (if interior prioritized)
- ~~FB Marketplace partner application~~ resolved: no dealer API exists since 2023 — assisted-posting MVP is the ceiling

## NEXT ACTION (one sentence)
Surface the gate's structured 422 reasons in the /mobile capture UI (API already
returns them) and run the full 60-second stranger-dealer test end-to-end on a
fresh account, then close the remaining owner-gated Stripe/pricing step.
