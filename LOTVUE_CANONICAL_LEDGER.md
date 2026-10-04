# LotVue360 — Canonical Position Ledger

> **Snapshot:** 2026-10-04 (America/New_York). This ledger is the current repo-level position; dated workstation/production proofs elsewhere remain historical until re-run. Do not treat local tests, configuration checks, or old artifacts as live integration or production proof.
>
> **Code line:** application source commit `08b9fcd` (`Secure walkaround intermediate artifacts`) on the fixed Arena branch `arena/01a0ffa3-car-360-viewer`. Commit `520c335` records the blocked-workflow result; this ledger refresh is documentation-only. Current application code has not changed since `08b9fcd`.

## Where we are

LotVue360 is an actively connected **local application codebase**, not a production-certified or paid product. Dealer Studio now bridges existing inventory/photo/video engines and routes rather than duplicating them. The latest walkaround hardening is locally tested, but no real ComfyUI/WAN/TTS or FFmpeg generation ran, and no current-source Windows/macOS build was produced.

| Area | Current status | Evidence / boundary |
|---|---|---|
| Current application source | **LOCAL VERIFIED** | `08b9fcd`; full suite **189 passed in 4.68 s** (subprocess **5.73 s**, peak child RSS **141.8 MiB**). Configured Ruff, Python/spec compilation, `pip check`, diff checks, and Node syntax check passed. Targeted walkaround/static/auth tests: **19 passed in 1.39 s**. |
| Dealer Studio route glue | **LOCALLY TESTED; not live** | Existing composite route, text-only video-ad draft route, and authenticated walkaround start/status route are surfaced in Dealer Studio. Start remains explicit; owner/tenant and queued/error behavior has local tests. No real media generation, provider call, or output-quality test. |
| Walkaround limits | **CODE + TEST VERIFIED** | Per source directory: scan at most **4,096** entries; worst case up to **8,192** across studio and exterior fallback. Retain at most **72** JPEG candidates; validate at most **4** selected images; cumulative validation is at most `min(200 MiB, configured photo-batch cap)`. Application photo defaults are **30 MiB/file** and **40 MP/image**. |
| Other application resource caps | **CODE-VERIFIED defaults; not load-tested** | Video upload **500 MiB**; photo batch **400 MiB**; background pool defaults **2 workers / 4 admitted jobs**; public viewer manifest **120 frames**. These are application caps, not disk quotas or production capacity guarantees. |
| Walkaround intermediates / publication | **MOCKED + HTTP TEST VERIFIED** | ComfyUI-returned filenames are confined beneath configured output and traversal/symlink targets rejected. LotVue working clips/audio/concat/stage live in a hidden per-job temporary directory; legacy public clips, `voiceover.wav`, and `concat.txt` are cleaned. Storage/auth gate returns 404 for hidden intermediates, even to an authenticated vehicle owner; final walkaround remains anonymously served for active inventory. FFmpeg must exit 0 and create a nonempty regular file before atomic replace. Failed/empty rerenders report error and preserve the old final. ComfyUI's own source-output retention remains controlled by ComfyUI. No actual FFmpeg binary or render ran here. |
| Hosted CI / current installer | **BLOCKED** | Latest app-source runs [CI 37177998115](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37177998115) and [desktop 37177998121](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37177998121) were blocked before runner steps because GitHub said an Actions budget prevents use; desktop build jobs were skipped. Docs-only runs 37178035732 / 37178035752 hit the same block. **No hosted test or installer artifact for current source.** |
| Last successful cross-platform artifact | **PASS ON OLDER SOURCE ONLY** | CI run [37140754753](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37140754753) and three-platform matrix [37140754758](https://github.com/abuz8studios-ship-it/car-360-viewer/actions/runs/37140754758) passed on `7fa13aa`. Artifact metadata lists Windows x64, Intel macOS x64, and Apple Silicon macOS arm64 ZIPs, unexpired through **2026-11-02 UTC**. These artifacts predate the newer Dealer Studio scene/script/walkaround UI and the `08b9fcd` hardening. No tag release was published. |
| Production, providers, billing, email | **NOT RE-VERIFIED** | No production-host probe or live provider test in this pass. Older notes report a Stripe `401 Expired API Key`, SMTP `535 BadCredentials`, and a production restart after an outage; those are historical last-known results, not a current measurement. No price is approved. |

## PRD acceptance position

The PRD's August 12 stranger-dealer run is **historical evidence only**. Its checkboxes are intentionally not rewritten as current passes. No new unaided dealer test or paid transaction has been recorded.

| PRD gate | Position now | What closes it |
|---|---|---|
| 1. Account registration and working dashboard | **OPEN / re-verify** | Fresh external dealer account on the actual live service; verify registration, login, and dashboard without agent help. |
| 2. Upload real walkaround video | **Historical pass 2026-08-12; re-run** | Fresh dealer uploads representative real-car footage on the current deployed version. |
| 3. Reject bad capture / accept good capture with clear reasons | **Historical pass 2026-08-12; re-run** | Repeat both paths on deployed product and review clarity of surfaced reasons. |
| 4. Produce usable 360 spin/background result | **Historical pass 2026-08-12; visual QA still open** | Render a current real vehicle and inspect the full-resolution spin, including identity, crop, seams, and backgrounds. |
| 5. Public VDP works | **Historical pass 2026-08-12; live service not checked** | Verify current public URL, access rules, and media behavior on the deployed domain. |
| 6. Google Vehicle Listings feed accepts listing | **Historical validator pass; live feed/ingestion not checked** | Re-run on current real inventory and verify Google registration/ingestion with owner-supplied public URL and credentials. |
| 7. Dealer pays and gets receipt | **OPEN / owner-gated** | Ahmad approves one canonical offer, reconciles every catalog, refreshes/configures valid Stripe credentials and webhook secret, then performs a controlled live transaction and verifies receipt, webhook, entitlement, and refund/rollback behavior. No agent may approve pricing. |
| End-to-end Definition of Done | **NOT COMPLETE** | One previously unaware dealer completes all seven steps, including payment, without help; the server survives reboot; a submitted lead reaches Ahmad by live-tested email. |

## Remaining work, in practical order

### P0 — unblock release evidence and paid acceptance

1. **Restore or wait for GitHub Actions budget availability.** Re-run CI and the three-platform desktop matrix on the current app source `08b9fcd` (or the then-latest code). Inspect the actual ZIPs/checksums and confirm Windows x64, Intel macOS, and Apple Silicon installers. The older `7fa13aa` artifact is not a substitute for current source. Then obtain owner acceptance on supported end-user machines; signing/notarization remains an owner decision and credential gate.
2. **Owner decision: canonical price.** Reconcile the written offers and code catalogs; keep billing and public price disclosure fail-closed until Ahmad approves. Current code/tests are safety checks, not a pricing decision.
3. **Owner supplies billing access and verifies a live transaction.** Refresh the expired Stripe key/webhook secret through the owner's secure configuration channel; test charge, receipt, webhook, entitlement, and reversal. No credentials should be sent in chat or committed.
4. **Run the current stranger-dealer acceptance test on the real service.** Verify registration/dashboard and repeat the recorded upload → quality gate → spin → VDP → Google-feed path, then the approved payment step. The 2026-08-12 pass does not prove current production behavior.
5. **Recheck production operations and lead delivery.** Owner verifies the live domain, process supervision across reboot, backups/recovery, and lead-email delivery. The last known outage/Stripe/SMTP observations are stale and need fresh checks; provide approved SMTP access before testing.

### P1 — quality, safety, and legal clearance

6. **Real-vehicle visual QA.** Inspect at least one complete, current full-resolution spin set. The current checkout contains no generated VIN set and no walkaround media was created in this pass.
7. **Plate privacy.** Plate-blur evidence is **synthetic only, not a full pipeline**. Measure false negatives/false positives on a labeled real-dealer dataset, check all relevant capture/render paths, and visually review results before marketing anonymization.
8. **Background-removal model rights.** No checkpoint is bundled/selected by default. Owner/legal reviewer must confirm provenance and commercial rights for the exact model before enabling or selling that path; acknowledgment in code is not legal approval.
9. **Data and customer-use permissions.** Obtain written DriveSmart permission before using its scraped dataset for demos/GTM; approve any prospect sample/outbound message; confirm customer-use and redistribution terms against the proprietary root `LICENSE` and packaged dependencies.
10. **External connectors.** Supply account-owner credentials/approvals and verify live Google feed ingestion, SMTP, SMS, marketplace, DMS, and other paid-provider connections as applicable. A local configuration indicator is not evidence of a live integration.

### P2 — optional product tracks and repo decisions

11. **iOS / app distribution:** iOS build needs Mac/cloud build service and Apple developer access. Android bundle is historical evidence; Google Play publication has not been proven in this branch and requires the owner's Play registration/account.
12. **Interior capture:** Ricoh Theta workflow remains hardware-gated if prioritized.
13. **Repo/document hygiene:** owner decides whether to consolidate BRAIN/GOAL/LEDGER conventions; backup source files should only be archived/removed after owner confirms recovery/deployment needs. Do not revive generative-mesh standard-tier work (intentionally killed) or claim Facebook Marketplace auto-posting (the assisted-posting pack is the supported ceiling in the current research).

## Historical proofs (not refreshed by this snapshot)

The 2026-08-12 live viewer/feed/stranger-dealer observations, 2026-09-02 sample and production/credential notes, and prior workstation claims remain in [`PRD.md`](./PRD.md), [`LEDGER_LOG.md`](./LEDGER_LOG.md), and [`ENGINEERING_AUDIT_2026-10-03.md`](./ENGINEERING_AUDIT_2026-10-03.md) with their original dates. This ledger does not upgrade them to current status.

## Immediate next action

**Restore Actions budget availability, then run the current-source CI and three-platform installer matrix; in parallel, the owner must choose the approved price and provide secure live-test access for Stripe, email, and production acceptance.**
