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
| 11 | Quality gate validate_video.py | DONE 08-12 | 316 lines; ORB-continuity is the discriminator (aspect-spread + bg-motion measured NON-discriminating: DS8097 spread 0.822, bg 0.154). CLI: DSREAL01 raw PASS / DS8097 FAIL 4/8 weak pairs. Live: /api/upload 422 capture_rejected (DS8097 photos) + 200 gate PASS (real walkaround video, 36 frames). Wired both branches, force=1 override. ALSO wired into /api/vehicle/{vin}/generate-3d photo+video branches (live 422 probed) — the exact route that fed COLMAP 2/N. UI surfaces reasons: /capture gatePanel + /mobile showGateReasons, both with explicit Upload-anyway override (served live, grep=2 each) | no |
| 12 | Bg asset pack | DONE 08-12 | Disk truth: 40 bgs 1920x1080 in static/backgrounds; studio.py was reading EMPTY static/studio_bg (gradient fallback) — load_bg now falls back to full library. All 40 rendered vs WDDEJ7EB9CA029569 full-res: halo 0.00% (metric injection-validated) + visual pass. NOTE: gradient backdrops, not photo scenes — photoreal pack = upgrade path | no |
| 13 | Hotspot/damage layer | DONE 08-12 | DB migration close_up; POST/GET carry condition_type/severity/note/close_up (400 without severity); viewer manifest merges DB rows (frame-resolved); /vdp/{vin}/damage-report printable, public 200, headless-Edge screenshot inspected | no |
| 14 | Google Vehicle Listings feed | DONE 08-12 | /api/feeds/google-vehicle-listings.xml public 200; rebuilt to published spec (fetched 08-12): +g:vin +dealership_name/address +trim, brand→make, color→exterior_color, XML-escaped, unsellable rows skipped; validator 155/155 items pass required attrs | no |
| 15 | FB Marketplace auto-post | RESEARCHED 08-12 | Marketplace dealer API/feeds DISCONTINUED 2023-01-30; only personal-account assisted posting (~10/day/acct). MVP live: /api/marketplace/facebook/{vin}/pack 200 (guardrailed listing pack) + catalog.csv for Meta vehicle ADS. No partner program to apply to | resolved: no API exists |
| 16 | Ricoh Theta interior | NOT STARTED | — | YES: hardware |
| 17 | Stripe checkout | NOT DONE | key expired | YES: refresh key |
| 18 | Pricing page abuz8ai.com | NOT DONE | needs #17 | no |
| 19 | DriveSmart written permission | NOT DONE | — | YES: owner obtains |
| 21 | Universal gallery→studio-spin (Spyne-clone lane) | DONE 08-13 | app/studio_spin.py + clip_fullcar.py: ANY dealer gallery → CLIP exterior/interior/detail → whole-car CLIP (SET-RELATIVE 0.72×median bar — absolute CLIP scores don't transfer between galleries) → matte geometry + set-relative saturation prune (ghost 8.8 vs set median 56; absolute bar killed silver cars) → ORB greedy chain order (chain prune only >12 frames; sparse 45°-apart sets link at 10-20 inliers legitimately) → studio composite → stepped mp4 (NO cross-angle flow — ghosts, re-measured). Proven zero-curation on 3 bodies/colors: Navigator 52→9, Accord 48→9 (interior-through-windshield ghost auto-dropped), Outback 22→8; all sheets eye-verified clean. KNOWN EDGE: CL63 doors-open frames still score as whole-car (29-angle spin has ~8 doors/detail frames) — prompt fine-tune or small detector = next quality step. Demo reel rnd\LOTVUE_SPYNE_CLONE_DEMO.mp4 delivered to owner | no |
| 20 | 60-SECOND STRANGER-DEALER TEST | PASSED 08-12 (all non-owner-gated steps) | Fresh account dealer60@ → register+login 200 → dashboard 200 (410KB, capture links) → real walkaround upload: gate PASS, 36 frames → order+interpolate 200 → /vdp/<vin> 200 LOCAL+PUBLIC with 360 viewer (screenshot) + real title from DB fallback (viewer_api meta.json→DB) → set-price 200 → vehicle appeared in Google feed, validator 156/156. Bad-capture path: 422 with reasons on same route. Test data cleaned after evidence. REMAINING: payment step = Stripe owner gate (#17→#18) | payment step only |

## OWNER GATES (Cowork cannot do these)
- iOS build (Mac or cloud build service)
- Apple Dev $99/yr + Google Play $25
- Stripe API key refresh (blocks #17→#18 pricing/checkout, the last big 60-second-test gap)
- DriveSmart written permission for scraped inventory as GTM dataset
- Ricoh Theta hardware (if interior prioritized)
- ~~FB Marketplace partner application~~ resolved: no dealer API exists since 2023 — assisted-posting MVP is the ceiling

## NEXT ACTION (one sentence)
Owner refreshes the Stripe key, then wire checkout + pricing page (#17→#18) —
the ONLY remaining step between a stranger dealer and paid signup; everything
else in the 60-second test passed on 08-12 with live evidence (row 20).
