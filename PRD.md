# LotVue360 — Product Requirements & Definition of Done

> This is a checklist of what "done" means, not a wishlist. DONE rows cite the commit, HTTP probe, or measurement that proved them. OPEN rows say exactly what's missing and who unblocks it.
>
> **Audit update: 2026-10-03.** This checkout was tested locally; the recorded August/September production and workstation checks remain historical evidence and were not re-run against live credentials or `lotvue.abuz8ai.com` in this audit. The Windows/macOS desktop workflow and installer scripts are implemented in this branch, but no hosted matrix run or desktop artifact exists yet; do not call the appliance complete until hosted builds and actual installs pass.

## The product, one line

A dealer uploads phone photos or a walkaround video; LotVue360 gates capture quality, builds the 360-photo spin and viewer, and can composite onto a studio backdrop only when an operator has separately approved and configured a commercially cleared removal model. The public VDP/feed still require owner setup and live-provider verification; product pricing remains undecided.

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

**"Done" is not code-complete or builds-clean.** It's: a real independent dealer, with zero help from Ahmad or any agent, uploads a real car and pays. The last recorded result is no completed paid stranger-dealer run and **$0 revenue reported**; this audit did not access billing/accounting data to refresh that number.

## Previously verified on the owner's workstation (historical evidence)

The checks below are retained with their original dates; this audit did not re-run them against the deployed service. The current checkout's `app/storage/` has no VIN outputs, so a generated spin could not be visually inspected here.

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

## Current Windows/macOS desktop-release acceptance — 2026-10-03

The session branch contains a PyInstaller launcher/package path, per-user Windows and macOS installers, a 3-platform GitHub Actions matrix, checksums, and CPython 3.11 dependency constraints. The hosted workflow is designed to install, self-test, and uninstall on Windows x64, macOS Intel, and Apple Silicon; pushing this session branch triggers it. The local source launcher passes its smoke test. **The hosted workflow has not yet run, no frozen artifact is available, and no Windows/macOS host has been used to install this branch's package.** The older Windows installer listed in the historical table is not evidence for these new artifacts.

The base bundle contains local Python/runtime libraries and FFmpeg/FFprobe only after the hosted build can download and verify platform binaries. No CUDA/ComfyUI/WAN/COLMAP infrastructure, provider accounts, credentials, signatures, notarization, or model weights are bundled. Background removal stays disabled unless the owner reviews and explicitly selects a checkpoint; the app's acknowledgment flag is not legal verification. The checkpoint's commercial license/provenance, actual FFmpeg license output, dependency notices, and end-user installer behavior remain release gates.

## Explicitly out of scope — do not resume

- **Generative-mesh 3D as a standard tier.** Tried, killed 2026-08-11 (68 of 118 generated GLB meshes were unusable and quarantined). Both reverse-engineered competitors (Spyne, GLO3D) also use photo-spin plus background replacement, not mesh generation — that independently confirms the pivot rather than exposing a shortfall. The real-3D Gaussian-splat path stays as the premium, real-footage-only tier.

## Blocking revenue — fix these before anything else

1. **Stripe checkout has not been verified live in this audit.** The last recorded test returned `401 Expired API Key`; this checkout has no `app/.env`, and no secret was accessed or changed. No payment can be confirmed until Ahmad rolls the key and runs a live test. *(Owner action.)*
2. **Pricing is undecided and several incompatible catalogs are present:**
   - `BRAIN.md`: $99/mo/dealer SaaS
   - `SALES_FUNNEL_FIRST_DOLLAR_2026-08-28.md`: $1,000 launch + $500/mo retainer
   - `SALES_FUNNEL_LOTVUE360_2026-08-28.md`: $1,000 sprint + $1,000/mo retainer
   - `app/main.py`: plan tables/page include $2.99, $19.99, $75/mo, $99.99/mo, and $299.99/mo values.

   The local code now defaults `LOTVUE_PRICING_APPROVED=0`: `/pricing` hides the price cards, `/api/plans` returns an empty catalog, and checkout/subscription/unlock/webhook paths stay disabled unless both pricing approval and the payment feature are enabled. A second fail-closed check also requires the checkout and subscription amounts to agree for single-render, starter, and dealership tiers; subscription checkout is restricted to the public dealership tier, and one-time unlocks use the checkout catalog amount rather than a separate literal. The legacy dealership values still conflict, so setting the approval flag alone cannot publish prices or create charges. This is a safe default, **not** a price decision. Ahmad must approve one canonical offer, reconcile the tables, and only then enable it.
3. **No sample has been sent to any prospect in the last recorded evidence.** The Select Auto sample was marked "FREE SAMPLE PRODUCED. NOT SENT" on 2026-09-02. Sending is Ahmad's gate (it's an outbound message on his behalf); the output is not present in this checkout.
4. **Lead-alert email has not been live-verified in this audit.** The last recorded Gmail SMTP app-password test returned `535 BadCredentials`; this checkout has no `app/.env`. *(Owner action: rotate/test the SMTP app password.)*
5. **Production uptime is unverified.** The last recorded note says the production server had roughly 3 weeks of 502 downtime through 2026-09-02 and was manually restarted. Whether `LOTVUE\\StartApp` is enabled and survives reboot needs a fresh owner-side check. *(Owner action; persistent system-config change.)*

## Owner-gated — blocked on Ahmad, not on build work

- iOS app (needs a Mac or a cloud build service)
- Google Play publishing ($25 registration fee, unpaid)
- Ricoh Theta interior-capture hardware (not started, hardware-gated)
- Written permission from DriveSmart Auto Sales to use its 200 scraped vehicles as a demo/GTM dataset (not yet obtained)

## Smaller open items

- **One VIN's full studio-grade spin set still needs full-resolution visual inspection.** The repository checkout has no generated VIN output under `app/storage/`; synthetic tests and source review cannot substitute for a visual review of a real spin set.
- **License-plate privacy remains OPEN.** The optional AI PhotoProcessor already calls the blur stage, and a synthetic 640×480 plate smoke test passes. The detector is a lower/center ROI heuristic with a regional Haar cascade and contour fallback; there are no representative real-dealer recall/false-negative measurements, and it is not proven to cover every standard capture/render path. Do not market this as reliable plate anonymization until measured on a labeled dataset and visually reviewed.
- **Background-removal model licensing remains OPEN.** The rembg package is only a Python runtime; no checkpoint is selected or included. Inference is gated on an operator-selected model plus explicit acknowledgment. The owner must verify commercial rights and weights provenance for the exact checkpoint; the acknowledgment is not legal approval. Until then, 360 output keeps photographed backgrounds and strict cut-out endpoints return a setup response.

## Repo hygiene — not blocking revenue, but real debt

- **Declared dependencies and release constraints are now present.** Root `requirements.txt`, `requirements-ai.txt`, `requirements-dev.txt`, `requirements-desktop.txt`, and `requirements-release-constraints.txt` replace the old inline-install-only setup and stabilize the CPython 3.11 CI/build package set. Local dry runs passed on Linux; the Windows/macOS wheel resolution remains unverified until the hosted workflow runs.
- **Dormant backup copies remain in `app/`.** The current checkout includes `main.py.bak-*`, `main_before_op6.py`, `main_original.py`, `main_v1.py`, and `.bak-*` copies of other modules. They are not runtime sources; archive or remove them only after Ahmad confirms there is no recovery/deployment dependency.
- **Doc naming doesn't match the BRAIN/GOAL/LEDGER convention used elsewhere.** There is no literal `GOAL.md` (`LOTVUE_GOAL_ARTIFACT.md` and `LOTVUE_CREATION_ARTIFACT.md` split that role between them), and two differently-shaped ledgers already exist (`LEDGER.md`, a dev-log, vs. `LOTVUE_CANONICAL_LEDGER.md`, a numbered capability table). This PRD and its siblings `README.md` / `LEDGER_LOG.md` are a second naming convention, not a replacement — **Ahmad needs to decide which convention the project runs on.**
- **License status corrected in this checkout:** tracked `LICENSE` is proprietary ABUZ8 LLC / All Rights Reserved, not MIT and not uncommitted. Owner/legal review should confirm the terms cover the intended hosted product and any future downloadable build.

## Local verification — 2026-10-03

The current checkout's automated checks are recorded in [`ENGINEERING_AUDIT_2026-10-03.md`](./ENGINEERING_AUDIT_2026-10-03.md). They establish source-level and isolated-test behavior only; they do not establish live production uptime, paid transactions, outbound email, Google validation against live inventory, or a stranger-dealer run.

## What "finished" looks like

Not "builds clean." Not "code complete." Finished is:
- A real, previously-unaware independent dealer completes the stranger-dealer test above, **including payment**, with zero help from Ahmad or any agent.
- The price they paid matches a single number that exists in exactly one place on disk.
- The production server survives a reboot on its own (scheduled task enabled, verified — not assumed).
- A lead that dealer submits reaches Ahmad by email without a live-tested credential failure in the way.
