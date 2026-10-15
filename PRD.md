# LotVue360 — Product Requirements & Definition of Done

> This is a checklist of what "done" means, not a wishlist. DONE rows cite the commit, HTTP probe, or measurement that proved them. OPEN rows say exactly what's missing and who unblocks it.
>
> **Historical audit note: 2026-10-03.** This checkout was tested locally; the recorded August/September production and workstation checks were not re-run against live credentials or `lotvue.abuz8ai.com`. At that point no hosted desktop artifact had been produced. The note is superseded by the dated status below; historical checklist evidence later in this PRD is retained rather than silently refreshed.
>
> **Current repository position: 2026-10-04.** The application source currently under review is commit `08b9fcd` on `arena/01a0ffa3-car-360-viewer`; commit `520c335` is a docs-only workflow record, and this ledger/PRD refresh is documentation-only as well. Local checks on the source commit report 189 tests passing, plus configured lint/compile/dependency/diff/inline-JavaScript checks. A three-platform GitHub desktop matrix did pass on older commit `7fa13aa` (runs [CI 37140754753](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37140754753), [desktop matrix 37140754758](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37140754758)); its Windows x64, Intel macOS, and Apple Silicon artifacts are available through 2026-11-02 UTC, but they predate the newer Dealer Studio scene/script/walkaround surface and the current walkaround hardening. Runs for `08b9fcd` ([CI 37177998115](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37177998115), [desktop 37177998121](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37177998121)) were blocked before runner steps because GitHub reported that an Actions budget prevents further use; build jobs were skipped. The docs refresh on `520c335` was blocked for the same reason (runs 37178035732 / 37178035752). Therefore **the current application source still has no hosted test result or installer artifact**.
>
> The August 12 stranger-dealer steps 1–6 remain dated historical evidence, not a current live-site check; payment step 7 remains open. No current production uptime, live billing/email/provider connection, actual ComfyUI/WAN/TTS generation, or real-vehicle quality pass is established here. No price is approved. See [`LOTVUE_CANONICAL_LEDGER.md`](./LOTVUE_CANONICAL_LEDGER.md) for the current status and ordered remaining gates.

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
7. [~] **Pays** — enters a card, is charged, gets a receipt. *Code complete + automated test passing (2026-10-09); never run against a live Stripe key — owner gate.*

Steps 2–6, plus account creation and dashboard (step 1), were run and passed end to end on 2026-08-12 (fresh account `dealer60@`: register + login 200 → dashboard 200 with capture links → real walkaround upload, gate PASS, 36 frames → 360 render → public VDP 200 with viewer, screenshot-verified → priced → Google feed validator 156/156). That specific test account's session is the evidence for step 1, not a standing guarantee — re-verify before quoting. Step 7 is now built (`app/billing.py`: Stripe Checkout subscription → signed webhook → plan activated → receipt on `/billing`) and exercised end to end by `tests/test_stranger_dealer.py` with Stripe faked and webhooks HMAC-signed exactly like Stripe's. It has still never been run against a live Stripe key, so it isn't checked yet.

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
| Billing / step 7 (code) | 2026-10-09: `app/billing.py`. Price set in one place (`LOTVUE_PRICE_MONTHLY_CENTS`, default $99/mo), Checkout in subscription mode, signed and idempotent webhook, `/billing` with receipts + Customer Portal, 402 paywall after `LOTVUE_FREE_VEHICLES`. Covered by `tests/` (21 passing). Not yet live-verified |
| Account security | 2026-10-09: salted PBKDF2 passwords (legacy sha256 upgraded on login), signup validation and duplicate check, the unsigned-webhook forgery hole and self-service `/api/set-plan` upgrade closed, `/` (inventory dashboard) no longer public, login open-redirect fixed |
| License-plate blur | 2026-10-09: `ai_studio/plate_blur.py` rewritten (old detector found 0/2 real plates; new one found 2/2 with 0 false positives across 8 real car photos plus the Select Auto studio sample). `POST /api/vehicle/{vin}/blur-plates`, with `LOTVUE_AUTO_PLATE_BLUR=1` to run after every finalize. Small sample: inspect one real dealer VIN before turning auto-blur on |
| Real-world dealer run | 2026-09-02: pulled 42 real listing photos from a named live dealer (Select Auto, Dayton OH) and produced a 7-angle studio spin; 4 of 7 angles judged clean on inspection |

## Historical Windows/macOS desktop-release snapshot — 2026-10-03 (superseded by current status above)

At the time of this 2026-10-03 snapshot, the branch contained the PyInstaller launcher/package path, per-user installers, a 3-platform GitHub Actions matrix, checksums, and CPython 3.11 constraints, but no hosted matrix run had completed. Later evidence is summarized in the current-status note above: the 3-platform matrix passed on older source commit `7fa13aa`, while the current app source commit `08b9fcd` could not start hosted CI/build jobs because of the GitHub Actions budget. The older Windows installer listed in the historical table is not evidence for this newer package line.

The verified older artifacts include local Python/runtime libraries and FFmpeg/FFprobe; they do not include CUDA/ComfyUI/WAN/COLMAP infrastructure, provider accounts, credentials, signatures, notarization, or model weights. Background removal stays disabled unless the owner reviews and explicitly selects a checkpoint; the app's acknowledgment flag is not legal verification. Owner installation acceptance, whether to sign/notarize, and commercial license/provenance for any checkpoint remain release gates.

## Explicitly out of scope — do not resume

- **Generative-mesh 3D as a standard tier.** Tried, killed 2026-08-11 (68 of 118 generated GLB meshes were unusable and quarantined). Both reverse-engineered competitors (Spyne, GLO3D) also use photo-spin plus background replacement, not mesh generation — that independently confirms the pivot rather than exposing a shortfall. The real-3D Gaussian-splat path stays as the premium, real-footage-only tier.

## Blocking revenue — fix these before anything else

1. **Stripe has not been verified live.** The checkout code is done (`app/billing.py` + the signed webhook in `app/main.py`), but the last recorded secret-key test returned `401 Expired API Key` (2026-09-02). *(Owner actions, in order:)*
   1. Roll the key and put it in `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY`.
   2. Add a webhook endpoint `<LOTVUE_PUBLIC_URL>/api/stripe/webhook` for `checkout.session.completed`, `invoice.paid`, `invoice.payment_failed`, `customer.subscription.updated`, `customer.subscription.deleted`, then put its signing secret in `STRIPE_WEBHOOK_SECRET`. Without it the webhook refuses every event (by design).
   3. Stripe → Settings → Customer emails: turn on "Successful payments" (the emailed receipt). Settings → Billing → Customer portal: activate it.
   4. Set `LOTVUE_FEATURE_PAYMENTS=1` and `LOTVUE_PRICING_APPROVED=1`, restart, then run one real $99 charge on a test account and refund it. Only then check step 7.
2. **Pricing — decided 2026-10-15 (owner): 5 free vehicles per account, lifetime (no reset), then $99/month Dealership plan.** The code has one source of truth (`LOTVUE_PRICE_MONTHLY_CENTS`=9900 and `LOTVUE_FREE_VEHICLES`=5, read by `app/billing.py`); the legacy plan tables in `app/main.py` read their dealership amount from it, and the legacy one-time credit packs stay off (`LOTVUE_CREDIT_PACKS=0`). The older documents below still carry superseded numbers:
   - `BRAIN.md`: $99/mo/dealer SaaS
   - `SALES_FUNNEL_FIRST_DOLLAR_2026-08-28.md`: $1,000 launch + $500/mo retainer
   - `SALES_FUNNEL_LOTVUE360_2026-08-28.md`: $1,000 sprint + $1,000/mo retainer
   - `app/main.py` (before 2026-10-15): legacy plan tables carried $2.99, $19.99, $75/mo, $99.99/mo, and $299.99/mo values. The dealership entries now read the one price from `app/billing.py`; the $2.99/$19.99 credit packs remain in code behind `LOTVUE_CREDIT_PACKS=0`, and the $299.99 enterprise row is not sold.

   Fail-closed defaults stay: with `LOTVUE_PRICING_APPROVED=0`, `/pricing` shows "Pricing is being finalized" with no amounts, `/api/plans` returns an empty catalog, the paywall is off, and checkout and the webhook refuse to run. Setting `LOTVUE_FEATURE_PAYMENTS=1` and `LOTVUE_PRICING_APPROVED=1` publishes the $99 plan and enables checkout; a live charge-and-refund is still required before step 7 is checked.
3. **No sample has been sent to any prospect in the last recorded evidence.** The Select Auto sample was marked "FREE SAMPLE PRODUCED. NOT SENT" on 2026-09-02. Sending is Ahmad's gate (it's an outbound message on his behalf); the output is not present in this checkout.
4. **Lead-alert email has not been live-verified in this audit.** The last recorded Gmail SMTP app-password test returned `535 BadCredentials`; this checkout has no `app/.env`. *(Owner action: rotate/test the SMTP app password.)*
5. **Production uptime is unverified.** The last recorded note says the production server had roughly 3 weeks of 502 downtime through 2026-09-02 and was manually restarted. Whether `LOTVUE\\StartApp` is enabled and survives reboot needs a fresh owner-side check. *(Owner action; persistent system-config change.)*

## Owner-gated — last recorded status; not re-verified in this pass

- iOS app (needs a Mac or a cloud build service; no current build proof in this branch)
- Google Play publishing (the last recorded note said the $25 registration fee was unpaid; current account/payment status is unknown)
- Ricoh Theta interior-capture hardware (previously marked not started; current hardware status unknown)
- Written permission from DriveSmart Auto Sales to use its 200 scraped vehicles as a demo/GTM dataset (previously not obtained; current approval status unknown)

## Smaller open items

- **Studio spin inspected at full resolution (2026-10-15): background replacement works, smoothness does not yet.** VIN 19UDE2F72MA005771 (11 dealer photos → 36 frames, background removed, `completed` in 58 s on CPU). In-between frames show ghosting (translucent wheels, smeared glass), the car's size/position jumps between angles, and the contact shadow floats below the tyres. The CarCutter acceptance test (4 photos → smooth 360° MP4, reference video pending) is the bar for fixing this.
- **Plate blur: before setting `LOTVUE_AUTO_PLATE_BLUR=1` in production, run `python -m ai_studio.plate_blur --vin <VIN>` on one real dealer VIN and inspect the frames at full res.** PR #2's rewritten detector (black-hat character verification) found 2/2 readable plates with 0 false positives on 8 real car photos plus the Select Auto sample (recorded 2026-10-09); unblurred originals are kept in a dot-prefixed `.orig_unblurred/` folder that `/storage` never serves.
- **License-plate privacy remains OPEN (historical note follows).** The optional AI PhotoProcessor already calls the blur stage, and a synthetic 640×480 plate smoke test passes. The detector is a lower/center ROI heuristic with a regional Haar cascade and contour fallback; there are no representative real-dealer recall/false-negative measurements, and it is not proven to cover every standard capture/render path. Do not market this as reliable plate anonymization until measured on a labeled dataset and visually reviewed.
- **Background removal — approved and bundled (2026-10-15).** Desktop builds ship `isnet-general-use` (primary) and `u2net` (fallback) under `app/models/rembg/`, pinned by SHA-256 and enabled in the settings template; the packaged self-test loads the model offline and cuts out a test image. Open owner question: IS-Net was trained on DIS5K, whose dataset terms restrict commercial use without the authors' permission (see `docs/THIRD_PARTY_NOTICES.md`); `u2net` is the Apache-2.0 alternative.

- **Security: `mobile_native/android/keystore.properties` (tracked in git) contains the Android upload-keystore store and key passwords, and `mobile_native/lotvue-upload.keystore` is tracked too.** The repo is private, but treat that password as exposed. Owner decision: move both out of git and, if the app goes to Play, enroll in Play App Signing so the upload key can be reset.

## Repo hygiene — not blocking revenue, but real debt

- **Declared dependencies and release constraints are present.** Root `requirements.txt`, `requirements-ai.txt`, `requirements-dev.txt`, `requirements-desktop.txt`, and `requirements-release-constraints.txt` define the CPython 3.11 CI/build package set. Local Linux checks pass, and the hosted matrix did resolve/build on Windows x64, Intel macOS, and Apple Silicon for older source `7fa13aa`; current source `08b9fcd` still needs the hosted matrix once the Actions budget permits.
- **Dormant backup copies removed (2026-10-15, owner-approved).** The twelve `main.py.bak-*`, `main_before_op6.py`, `main_original.py`, `main_v1.py`, and other `.bak-*` copies in `app/` (36,039 lines, none imported or packaged) were deleted; they remain recoverable from git history.
- **Doc naming doesn't match the BRAIN/GOAL/LEDGER convention used elsewhere.** There is no literal `GOAL.md` (`LOTVUE_GOAL_ARTIFACT.md` and `LOTVUE_CREATION_ARTIFACT.md` split that role between them), and two differently-shaped ledgers already exist (`LEDGER.md`, a dev-log, vs. `LOTVUE_CANONICAL_LEDGER.md`, a numbered capability table). This PRD and its siblings `README.md` / `LEDGER_LOG.md` are a second naming convention, not a replacement — **Ahmad needs to decide which convention the project runs on.**
- **License status corrected in this checkout:** tracked `LICENSE` is proprietary ABUZ8 LLC / All Rights Reserved, not MIT and not uncommitted. Owner/legal review should confirm the terms cover the intended hosted product and any future downloadable build.

## Historical local verification snapshot — 2026-10-03

The 2026-10-03 source audit is recorded in [`ENGINEERING_AUDIT_2026-10-03.md`](./ENGINEERING_AUDIT_2026-10-03.md); the newer local result and remaining gates are in the current-status note and [`LOTVUE_CANONICAL_LEDGER.md`](./LOTVUE_CANONICAL_LEDGER.md). Source tests establish isolated behavior only; neither those results nor the newer 2026-10-04 tests establish live production uptime, paid transactions, outbound email, Google validation against live inventory, or a new stranger-dealer run.

## What "finished" looks like

Not "builds clean." Not "code complete." Finished is:
- A real, previously-unaware independent dealer completes the stranger-dealer test above, **including payment**, with zero help from Ahmad or any agent.
- The price they paid matches a single number that exists in exactly one place on disk.
- The production server survives a reboot on its own (scheduled task enabled, verified — not assumed).
- A lead that dealer submits reaches Ahmad by email without a live-tested credential failure in the way.
