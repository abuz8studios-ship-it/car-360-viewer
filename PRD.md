# LotVue360 — Product Requirements & Definition of Done

> This is a checklist of what "done" means, not a wishlist. DONE rows cite the commit, HTTP probe, or measurement that proved them. OPEN rows say exactly what's missing and who unblocks it.

## The product, one line

A dealer uploads phone photos or a walkaround video of a car; LotVue360 gates the input for quality, removes the background, composites it onto a studio backdrop, builds a 360-degree spin viewer, and publishes a public VDP plus a Google Vehicle Listings feed — at a fraction of Spyne/CarCutter/GLO3D/MotorCut pricing.

## Definition of done — the stranger-dealer test

A dealer who has never seen the product, unaided, end to end:

1. [ ] Goes to lotvue.abuz8ai.com, creates an account, lands on a working dashboard (not a 401, not blank)
2. [x] Uploads a walkaround video of a real car
3. [x] Bad capture → rejected with a clear, structured reason. Good capture → proceeds.
4. [x] Gets back a rendered 360 spin with the background replaced, in minutes not hours
5. [x] Sees the vehicle live on a public VDP URL they can share
6. [x] Sees it appear as a valid entry in the Google Vehicle Listings feed
7. [ ] **Pays** — enters a card, is charged, gets a receipt

Steps 2–6, plus account creation and dashboard (step 1), were run and passed end to end on 2026-08-12 (fresh account `dealer60@`: register + login 200 → dashboard 200 with capture links → real walkaround upload, gate PASS, 36 frames → 360 render → public VDP 200 with viewer, screenshot-verified → priced → Google feed validator 156/156). That specific test account's session is the evidence for step 1, not a standing guarantee — re-verify before quoting. Step 7 has never been attempted against a live Stripe key.

**"Done" is not code-complete or builds-clean.** It's: a real independent dealer, with zero help from Ahmad or any agent, uploads a real car and pays. That has not happened. Revenue to date: **$0**.

## Shipped and working (disk-verified)

| Capability | Evidence |
|---|---|
| Cold boot / process supervision | Watchdog relaunches uvicorn after a kill; `/` returns 200 locally and publicly (re-probed 2026-08-12) |
| 360 photo-spin viewer (3-tier) | `/api/viewer`, `/vdp`, `/embed` all live 200 |
| Real-3D Gaussian-splat reconstruction (premium tier) | COLMAP solved 150/150 frames, Brush produced `scene.ply`, on a real walkaround (VIN DSREAL01) |
| Dealer inventory import | 200 real vehicles scraped and imported from a named dealer (DriveSmart Auto Sales) |
| CRM kanban | `/crm` live, dealer-gated (401 without auth) |
| Windows desktop installer | `installer/LotVue360-Setup-2.1.0.exe`, 311MB, cold-boot verified |
| Signed Android app bundle | Signed 4.7MB `.aab`, keystore on disk (`mobile_native/lotvue-upload.keystore`) — not yet published (Play's $25 registration fee unpaid) |
| AI background replacement ("studio" layer) | `app/studio.py` — segment → alpha refine → contact shadow → composite → optical-flow interpolation; shadow strength pixel-measured against the background, not eyeballed |
| Input-quality gate | `app/validate_video.py` (316 lines) — ORB-continuity is the proven walkaround-vs-synthetic discriminator; live 422 on a rejected real-dealer photo set, live 200 pass on a real walkaround video; wired into both the upload route and the 3D-generation route, with a `force=1` override and UI-surfaced rejection reasons |
| Studio background asset pack | 40 backdrops, rendered against a full-resolution real vehicle at 0.00% halo |
| Hotspot / damage-report layer | DB-backed; printable `/vdp/{vin}/damage-report`, public 200, screenshot-inspected |
| Google Vehicle Listings feed | `/api/feeds/google-vehicle-listings.xml` public 200; validator-passing (156/156 items at last measurement) |
| Facebook Marketplace | Researched, not built as automation — Meta discontinued the dealer feed API in 2023; shipped an assisted-posting pack instead (`/api/marketplace/facebook/{vin}/pack`), which is the real ceiling, not a gap |
| Universal gallery→spin lane ("Spyne-clone" mode) | `app/studio_spin.py` — turns *any* dealer's existing gallery photos into a spin with zero manual curation; proven clean across 3 vehicles of different bodies/colors (Navigator, Accord, Outback) |
| Stranger-dealer 60-second test | PASSED 2026-08-12 for every step except payment (see Definition of Done above) |
| Real-world dealer run | 2026-09-02: pulled 42 real listing photos from a named live dealer (Select Auto, Dayton OH) and produced a 7-angle studio spin; 4 of 7 angles judged clean on inspection |

## Explicitly out of scope — do not resume

- **Generative-mesh 3D as a standard tier.** Tried, killed 2026-08-11 (68 of 118 generated GLB meshes were unusable and quarantined). Both reverse-engineered competitors (Spyne, GLO3D) also use photo-spin plus background replacement, not mesh generation — that independently confirms the pivot rather than exposing a shortfall. The real-3D Gaussian-splat path stays as the premium, real-footage-only tier.

## Blocking revenue — fix these before anything else

1. **Stripe checkout is dead.** The secret key in `app/.env` returns `401 Expired API Key` on a live test. No payment can be collected until the key is rolled. *(Owner action: roll the key in the Stripe dashboard.)*
2. **Pricing is undecided — three different numbers exist on disk right now:**
   - `BRAIN.md`: $99/mo/dealer SaaS
   - `SALES_FUNNEL_FIRST_DOLLAR_2026-08-28.md`: $1,000 launch + $500/mo retainer
   - `SALES_FUNNEL_LOTVUE360_2026-08-28.md`: $1,000 sprint + $1,000/mo retainer
   No offer goes out with a number Ahmad hasn't confirmed.
3. **No sample has been sent to any prospect.** The Select Auto sample (2026-09-02, real dealer, real car) is built and sitting in `growth/select-auto-sample-2026-09-02/` — status is explicitly "FREE SAMPLE PRODUCED. NOT SENT." Sending is Ahmad's gate (it's an outbound message on his behalf).
4. **Lead-alert email is broken.** The Gmail SMTP app password in `app/.env` returns `535 BadCredentials` on a live test — no lead-notification email currently fires. *(Owner action: generate a new app password.)*
5. **The production server had roughly 3 weeks of unplanned downtime** (502, from on/around 2026-08-12 to 2026-09-02) because the Windows scheduled task that auto-starts it (`LOTVUE\StartApp`) was left Disabled. It was manually restarted 2026-09-02; whether the task itself has since been re-enabled needs a fresh check, not an assumption. *(Owner decision: re-enable — it's a persistent system-config change.)*

## Owner-gated — blocked on Ahmad, not on build work

- iOS app (needs a Mac or a cloud build service)
- Google Play publishing ($25 registration fee, unpaid)
- Ricoh Theta interior-capture hardware (not started, hardware-gated)
- Written permission from DriveSmart Auto Sales to use its 200 scraped vehicles as a demo/GTM dataset (not yet obtained)

## Smaller open items

- One VIN's full studio-grade spin set still needs full-resolution visual inspection (BRAIN.md's own delivery contract has this unchecked)
- License-plate blur is still open (the rest of the hotspot/damage layer is done; plate blur specifically is not)

## Repo hygiene — not blocking revenue, but real debt

- **No `requirements.txt` or `pyproject.toml` anywhere in the repo.** Dependencies exist only as an inline pip-install comment in `app/main.py` and in `README.md`. A from-scratch environment is not reproducible from disk alone without reverse-engineering imports.
- **`app/` carries at least 7 parallel copies of the main entry point** (`main.py.bak`, `.bak-classifier`, `.bak-dealer-import`, `.bak-preglue`, `main_backup_20260318.py`, `main_before_op6.py`, `main_original.py`, `main_v1.py`) plus `.bak` variants of `commercial_pipeline.py`, `gaussian_3d.py`, `h_dealer_import.py`, and `mobile_app.py` — all sitting live alongside the real files.
- **Doc naming doesn't match the BRAIN/GOAL/LEDGER convention used elsewhere.** There is no literal `GOAL.md` (`LOTVUE_GOAL_ARTIFACT.md` and `LOTVUE_CREATION_ARTIFACT.md` split that role between them), and two differently-shaped ledgers already exist (`LEDGER.md`, a dev-log, vs. `LOTVUE_CANONICAL_LEDGER.md`, a numbered capability table). This PRD and its siblings `README.md` / `LEDGER_LOG.md` are a *second* naming convention layered on top of that, not a replacement — **Ahmad needs to decide which convention this project runs on going forward.** Nothing old has been deleted; `BRAIN.md`, `LEDGER.md`, `LOTVUE_CANONICAL_LEDGER.md`, `LOTVUE_GOAL_ARTIFACT.md`, and `LOTVUE_CREATION_ARTIFACT.md` are all still on disk, unchanged.
- **Licensing is undecided.** A `LICENSE` file (MIT, uncommitted, added 2026-08-19) sits at the repo root and has never been reconciled with the commercial pricing above — an MIT grant would let anyone copy and resell this. Flagged here, not resolved.
- **Uncommitted work as of this pass:** `LEDGER.md` modified; `LICENSE`, two `SALES_FUNNEL_*.md` files, `docs/`, `growth/`, and `.codegraph/` untracked.

## What "finished" looks like

Not "builds clean." Not "code complete." Finished is:
- A real, previously-unaware independent dealer completes the stranger-dealer test above, **including payment**, with zero help from Ahmad or any agent.
- The price they paid matches a single number that exists in exactly one place on disk.
- The production server survives a reboot on its own (scheduled task enabled, verified — not assumed).
- A lead that dealer submits reaches Ahmad by email without a live-tested credential failure in the way.
