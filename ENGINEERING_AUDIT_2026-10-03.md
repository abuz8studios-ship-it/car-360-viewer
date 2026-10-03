# Engineering Audit — LotVue360

**Audit date:** 2026-10-03
**Scope:** the checked-out repository, local automated tests, local source/runtime probes, and synthetic-only microbenchmarks.
**Disposition:** **Local code checks and hosted desktop smoke tests pass; this is not a production-readiness or live-integration sign-off.**

## Executive summary

The checkout now has reproducible base/dev dependency files, an automated regression suite, safer tenant/path handling, bounded upload parsing, bounded background work, and explicit local verification. The audit also found and fixed two issues that deserved fail-closed behavior:

1. **Pricing was contradictory across catalogs and Stripe routes.** Prices remain unapproved. Public price disclosure and charge paths default closed. A second guard requires the checkout and subscription amounts to agree for the public single, starter, and dealership tiers. Checkout/unlock amounts now derive from the catalog, subscription amounts are generated from the monthly catalog amount, and legacy non-public subscription tiers are rejected. The existing dealership amounts still disagree (**$75.00 vs. $99.99**), so even setting the approval flag by itself does not publish prices or permit a charge.
2. **An async CSV/DMS import path performed synchronous database-backed authentication on the event loop.** The auth lookup now runs in the worker threadpool; a thread-identity regression test covers both import routes. The active app exposes 192 route operations: 157 synchronous handlers and 35 async handlers. The async handlers were reviewed for blocking database, filesystem, network, and image-processing work; those operations are delegated to worker threads or bounded background jobs, while request-body reads remain async.

The current automated suite reports **136 passed in 2.41 s**. A timed local run measured **2.83 s wall time** and **138.7 MiB peak child RSS**. Configured Ruff, Python compilation, `git diff --check`, workflow-YAML parsing, release-constraint resolution, and installer-shell syntax checks pass. A clean-source launcher smoke test completed in **2.861 s** with a **148.9 MiB child-process peak RSS**, generated all 40 backdrops, verified 8 compatibility scenes and the local capability snapshot, and confirmed that the snapshot makes no external probes. The performance numbers are deliberately narrow: synthetic plate-blur and catalog-startup checks plus in-process TestClient routes. **They are not full upload, image-processing, 360-render, or Gaussian-splat pipeline measurements.**

Hosted verification now passes for the tested release commit: CI run **37129049080** and desktop matrix run **37129049040** (commit `861e78e`). The matrix built and uploaded Windows x64, Intel macOS x64, and Apple Silicon macOS arm64 artifacts. Native runner smoke tests installed and launched the Windows/macOS bundles; Windows also verified uninstall and retained user data. The macOS tests removed the installed app bundle after the self-test. Artifact downloads attempted from this sandbox failed at GitHub's Azure blob endpoint with TLS EOF, so I have not independently downloaded or inventoried the uploaded ZIPs. The tested workflow runs, not a separate local artifact readback, are the current platform evidence. No tag-triggered GitHub Release was published.

There is no `app/.env`, no GPU/CUDA runtime, no local FFmpeg/FFprobe binary, and no vehicle output files under `app/storage/`. Production uptime, real payments, email delivery, external feeds, real image quality, owner-run end-user installation, code signing/notarization, and commercial model clearance remain unverified.

## Scope and method

- Read the repository PRD/README and treated historical workstation, production, and dealer-run notes as dated evidence—not fresh verification.
- Inventoried the registered FastAPI/OpenAPI operations and active route-handler definitions. Historical `main_*` backup modules are not counted as active route modules.
- Reviewed async route handlers and the synchronous I/O boundaries. Added regression tests where the audit found a blocking auth/database call on an async import path.
- Ran the full local pytest suite, the repository-configured Ruff check, `compileall`, and `git diff --check`; additionally parsed the GitHub Actions YAML, dry-ran both constrained pip installations, checked `pip`, and syntax-checked the macOS shell installers.
- Exercised `/pricing` and `/api/plans` through FastAPI `TestClient`, benchmarked the plate-blur function against a synthetic 640×480 image, and cold-started the source launcher after deleting its generated catalog. No remote service was contacted by those checks.
- Reviewed the desktop source staging, license-gate logic, per-user install/uninstall scripts, and three-platform workflow. Hosted run `37129049040` passed the Windows x64, macOS Intel, and Apple Silicon build/install/runtime smoke matrix and uploaded all three artifacts. No frozen platform artifact was built locally; sandbox downloads of the hosted artifacts failed with TLS EOF at the Azure blob endpoint, so there was no independent local ZIP readback.

## Verification metrics

| Check | Result | Measurement boundary |
|---|---:|---|
| Automated tests | **136 passed** | Local pytest suite; integrations are mocked/isolated where appropriate |
| Test modules | **35** | `tests/test_*.py` |
| Timed pytest run | **2.83 s wall, 138.7 MiB peak child RSS** | Linux sandbox, CPython 3.11; measured in a subprocess |
| Clean source-launcher smoke | **2.861 s, 148.9 MiB peak RSS** | Cold startup generated the 40 backgrounds; Python 3.11.2, Linux x86-64, 2 reported CPUs; not a frozen build |
| Configured Ruff | **Pass** | Project policy in `ruff.toml`: `ASYNC`, `E722`, `F811`, `F821`, `F823` |
| Python compile check | **Pass** | `python -m compileall -q app tests scripts` |
| Whitespace/conflict check | **Pass** | `git diff --check` |
| Workflow syntax | **Pass** | PyYAML parse plus assertions for Windows x64, macOS x64/arm64, and tag-only publishing |
| Dependency resolution | **Pass locally** | `pip install --dry-run` for dev and desktop requirements using the checked-in CPython 3.11 constraints; Linux only |
| Intel macOS wheel compatibility | **Pass** | Downloaded CPython 3.11 x86-64 wheels for cryptography 48.0.1, cffi 2.1.1, ONNX Runtime 1.23.2, llvmlite 0.45.1, Numba 0.62.1, and NumPy 2.3.5; hosted Intel build then completed |
| Hosted CI | **Pass, run 37129049080** | Commit `861e78e`; GitHub Actions tests and lint |
| Hosted desktop matrix | **3/3 pass, run 37129049040** | Windows x64, macOS x64, and macOS arm64 built; native installer/runtime smoke passed; each artifact uploaded |
| GitHub artifact-store metadata | **3 artifacts uploaded** | 30-day retention. Outer artifact ZIP sizes: Windows 324,042,317 bytes; macOS x64 286,904,672 bytes; macOS arm64 277,239,587 bytes. These are transport-wrapper sizes, not measured inner installer ZIP sizes |
| Independent artifact download/readback | **Not completed** | `gh run download` failed with EOF against Azure blob storage; `curl --range` failed with `SSL_ERROR_SYSCALL`. The native runners extracted and smoke-tested the generated ZIPs before upload |
| Installer shell syntax | **Pass** | `bash -n install-lotvue.sh Install-LotVue360.command`; does not execute macOS system calls |
| Exhaustive Ruff diagnostic | **7,157 findings across 119 paths** | `ruff check . --select ALL`; not the configured project gate; see lint note below |
| OpenAPI operations | **192** across **184** path templates | 118 GET, 65 POST, 7 DELETE, 2 PUT |
| Active route-handler definitions | **192** | 157 synchronous, 35 asynchronous, across the app and registered extension modules |

### Local route smoke timings

100 requests per route after 10 warmups, measured inside FastAPI `TestClient` (no real TCP connection, reverse proxy, internet, or production worker):

| Route | Responses | p50 | p95 | Max |
|---|---:|---:|---:|---:|
| `GET /pricing` | 100 × 200 | 2.516 ms | 2.753 ms | 3.160 ms |
| `GET /api/plans` | 100 × 200 | 2.490 ms | 2.920 ms | 3.373 ms |

The default responses were also checked: `/pricing` contains “Pricing is being finalized”; `/api/plans` returns `{"status":"pending_approval","plans":[]}`.

### Cold local-app/catalog startup

- Starting `app/lotvue_standalone.py --self-test` from a clean source tree generated all 40 1920×1080 background images and thumbnails, registered the app, verified 8 scenes, and checked the configuration-only health snapshot in **2.861 s wall time** with **148.9 MiB child-process peak RSS**.
- A pre-optimization clean run took **12.420 s** and **142.8 MiB** peak RSS. Replacing approximately 80 million Python pixel assignments with a NumPy row-gradient/broadcast reduced this specific cold-start measurement by **4.34×**; peak RSS increased by **6.1 MiB**. The two runs are same-sandbox smoke measurements, not a cross-platform benchmark or a PyInstaller startup result.

### Synthetic-only plate-blur microbenchmark

- Input: a generated **640×480** RGB image with a simple synthetic plate-like rectangle and text.
- Runs: **10 warmups + 100 timed calls** to `blur_license_plate` only.
- Runtime: Python 3.11.2 on a Linux x86-64 sandbox with **2 reported CPUs**; GPU/CUDA was unavailable.
- Latency: **6.794 ms min, 7.243 ms median, 8.216 ms p95, 8.880 ms max**; median-derived rate **138.1 calls/s**.
- Process RSS around the operation: **109.1 MiB before**, **109.6 MiB after**, **109.6 MiB process peak** for this imported-app/synthetic test process.

**Interpretation:** this is a small CPU smoke test and a process-level memory observation, not an image-pipeline benchmark. It does not measure detection accuracy, recall, false negatives, real dealer photos, high-resolution frames, memory at the configured pixel limit, uploads, concurrent processing, or end-to-end 360 generation. The simple synthetic test proves only that the function changes pixels in the expected synthetic region.

## Resource, throughput, and capability controls

| Control | Default in this checkout | Behavior / boundary |
|---|---:|---|
| Exterior video frames | **36** | Nominal 10° steps for one 360 turn |
| Video upload | **500 MiB** | Streamed to disk in bounded 1 MiB chunks; route middleware includes multipart overhead |
| Individual photo | **30 MiB** | Read in bounded chunks; decoded-image pixel limits also apply |
| Photo batch | **400 MiB** | Enforced across the batch; photo capture routes cap at 72 photos |
| Uploaded-image pixel count | **40,000,000 px** | Default decode guard; one uncompressed 40 MP RGB buffer alone is about 114.4 MiB, before processing copies |
| Concurrent large/file-upload requests | **2 per process** | Nonblocking admission limit; excess work receives 503 with `Retry-After: 30`; configurable with `LOTVUE_MAX_CONCURRENT_UPLOADS` (bounded 1–8) |
| Managed background workers | **2 threads** | Configurable 1–4 |
| Background jobs admitted | **4** | Includes running and queued work; configurable 2–16, bounded by worker count |
| Public viewer frame list | **120 frames** | `/viewer/{job_id}` |
| Vehicle-page media enumeration | **up to 300 names per media directory** | `heapq.nsmallest` bounds retained filenames rather than sorting the entire directory |
| General API request body | **2 MiB** | Explicit media/import routes receive larger route-specific ceilings |
| Dealer CSV import | **10 MiB file / 5,000 rows** | Parsed input and imported database writes are bounded; importer performs DB work off the event loop |
| Dealer-feed photo import | **100 photos; 30 MiB each; 400 MiB batch** | Separate importer limits; imported source images default to a 16 MP pixel cap |

The upload admission limit and background-job limit are **process-local**. With multiple Uvicorn workers, aggregate capacity is approximately the per-process limit multiplied by worker count; deployment memory, disk, reverse-proxy, and worker settings still need to be sized and load-tested. Per-request caps do not establish a total disk quota or retention policy.

## Findings and changes

### 1. Pricing and charge safety — resolved safely; owner decision remains open

- `LOTVUE_PRICING_APPROVED` defaults to `0`; `LOTVUE_FEATURE_PAYMENTS` defaults to `0`.
- Public `/pricing` price cards stay hidden and `/api/plans` returns an empty pending-approval catalog unless approval is set **and** the mapped checkout/subscription amounts match.
- Checkout, subscription, render-unlock, and webhook charge paths are guarded by payment enablement and the pricing approval/consistency check.
- The visible price page is populated from the checkout catalog instead of separate amount literals. One-time unlock amount derives from the single-render catalog. Monthly subscription amount derives from the subscription catalog; only the public dealership tier is accepted, and the old non-public enterprise tier is rejected.
- The checkout and subscription dealership prices currently conflict (**$75.00** in `PLANS`; **$99.99** in `PRICING_PLANS`). Therefore the current catalog-consistency result is false and approval cannot be accidentally enabled by changing only the environment flag.
- Tests cover hidden public pricing, accidental feature enablement, the mismatched-catalog fail-closed behavior for checkout/subscription/unlock/webhook routes, dynamic price rendering under an aligned synthetic fixture, subscription amount construction using a mocked Stripe SDK, and rejection of the legacy enterprise tier.

**No price was selected or approved on the owner's behalf.** Reconcile the written offer and all price tables first; only then enable prices, payments, and live Stripe verification.

### 2. Event-loop blocking — audited and regression-covered

- Active route inventory: **35 async** and **157 sync** handler definitions. FastAPI executes ordinary synchronous handlers in its worker pool.
- Async handlers await request-stream/upload reads. Blocking DB, filesystem, synchronous network probes, image operations, and longer generation work are offloaded via `run_in_threadpool` or submitted to the bounded background executor.
- The CSV and DMS import handlers previously performed `_actor_email()`—including a SQLite user lookup—directly in the async handler. Both now call it in the threadpool. A regression test checks that auth and import functions execute off the event-loop thread.
- Additional event-loop regression tests cover exterior-image processing, ComfyUI status, and public-lead work.

This was a source review plus focused tests, not a formal proof against every future helper call. Keep the route policy in review and add regression coverage when adding async endpoints.

### 3. Desktop release scaffold and license gates — hosted matrix passed; owner acceptance remains open

- The repository contains a per-user Windows installer/uninstaller, a per-user macOS installer, an in-process frozen launcher, a three-host GitHub Actions matrix (Windows x64, macOS Intel, macOS Apple Silicon), SHA-256 sidecars, and Python 3.11 release constraints. Branch pushes run the matrix; only a `v*` tag can publish a GitHub Release.
- Hosted CI run **37129049080** passed on commit `861e78e`. Desktop run **37129049040** passed all three builds and native smoke tests: Windows x64 installed, launched the frozen runtime, verified the self-test log, uninstalled, and retained per-user data; macOS x64 and arm64 installed the `.app`, passed the frozen self-test, and removed the app bundle. All three GitHub Actions artifacts uploaded successfully. The Intel macOS job took 9m01s, Windows 7m13s, and Apple Silicon 3m39s (jobs ran in parallel).
- Two Intel macOS dependency gaps were diagnosed across the hosted retries: newer `cryptography` no longer publishes Intel Mac wheels, and PyPI no longer supplies x86-64 macOS `llvmlite` wheels after 0.45.1. The latter fell back to a source build and failed because LLVM CMake files were unavailable. Constraints now use wheel-compatible `cryptography==48.0.1`, `llvmlite==0.45.1`, `numba==0.62.1`, `numpy==2.3.5`, and `onnxruntime==1.23.2` on Intel Mac. Platform-marker regression tests protect this stack while other platforms retain current pins. The follow-up native Intel runner completed dependency installation, build, smoke test, and artifact upload.
- The builder's FFmpeg stage ran on each host, refuses nonfree or unidentified builds, writes the detected license and `-version`/`-buildconf` notices, and records binary SHA-256 values. Each generated ZIP was extracted and its installed app was exercised before upload. Sandbox attempts to download the uploaded artifacts failed with Azure blob TLS EOF/`SSL_ERROR_SYSCALL`; I therefore have not independently read back the archive manifests, notices, checksums, or full file inventories. GitHub Actions reports artifact-store wrapper sizes, not direct measurements of the nested installer ZIPs. The tag-only publish job was skipped; no GitHub Release was created.
- Local checks: the builder CLI/help, workflow YAML/matrix assertions, Linux dependency-resolution dry runs, Intel wheel downloads, installer shell syntax, and the **source** launcher self-test passed. The source test verified 41 background entries, 8 scene previews, health-gate consistency, and no external probes. The local sandbox itself has no FFmpeg/FFprobe executable; the hosted build is the binary/license-gate evidence.

- `rembg` inference now fails closed unless `LOTVUE_REMBG_MODEL` is explicitly set and `LOTVUE_REMBG_LICENSE_ACCEPTED=1` is explicitly acknowledged. The first-run template leaves both off; active paths use the adapter; old backup modules with direct imports are excluded from release staging. Four tests cover the gate and a 503 response with temporary-file cleanup. The acknowledgment is not a license check, no model weights are bundled, and no model's commercial terms or inference quality are approved/tested.
- Before customer-facing distribution, the owner should independently download and inspect the uploaded archives, review the build manifest/FFmpeg notices and a dependency-license or SBOM report, and test install/update/removal on representative end-user Windows/macOS machines. Code signing, Windows trust reputation, and macOS notarization remain owner-provided and are not part of this workflow.

### 4. Upload, memory, and background admission — bounded; production load remains unmeasured

- Request bodies and multipart file/field counts are checked before Starlette route parsing; ordinary requests default to a 2 MiB ceiling.
- Large video uploads are streamed to a temporary file and atomically renamed; image batches are read/processed one file at a time and capped by per-file, batch, and pixel limits.
- Large/file-upload routes are now limited to two concurrent requests per process by default; an overload returns 503 plus a 30-second retry hint. A middleware regression test proves rejection under saturation and verifies that the slot is released afterwards.
- The processing executor admits at most four jobs with two worker threads by default, deduplicates identical per-key jobs, and returns retryable busy/full responses rather than accumulating unbounded background submissions.

These controls reduce unbounded per-request and background work. They do not replace reverse-proxy timeouts, process/container memory limits, storage quotas, production monitoring, or a concurrent-load test.

### 5. Public media/viewer behavior — locally covered; no real output to inspect

Vehicle IDs, active-vehicle state, tenant ownership, path containment, symlink boundaries, filename allowlists, image size and frame caps, and public video URLs have targeted tests in the suite. Frame enumeration uses bounded `heapq.nsmallest` limits. No VIN output exists in this checkout, so there was no real spin set to render, compare, or visually inspect.

## Remaining risks and owner-gated work

1. **Pricing decision and live billing:** the owner must approve one canonical offer, reconcile every checkout/subscription display and amount, configure a valid Stripe key/webhook secret, then execute a controlled test transaction and verify its receipt, webhook, idempotency, and entitlement. No live Stripe transaction was made.
2. **Production state:** no production host was accessed. Current uptime, deployment config, restart behavior, and recovery from the previously recorded outage are unverified.
3. **Outbound email:** SMTP credentials are absent here; lead-alert delivery and credentials were not tested live.
4. **External integrations:** Google feed validation against live dealer inventory, NHTSA/network services, DMS credentials, ComfyUI/Ollama endpoints, and social/listing integrations were not live-tested in this audit.
5. **Real-capture quality:** `app/storage/` has **0 files** and `app/.env` is absent. Real-dealer photo quality, output latency, failure rate, and 360 visual quality remain unmeasured; historical workstation runs are not current checkout evidence.
6. **License-plate privacy:** only a synthetic blur smoke test passed. There is no labeled real-dealer dataset or measured recall/false-negative rate, and standard-path coverage is not proven. Do not advertise guaranteed plate anonymization.
7. **High-resolution/concurrent resource use:** the 40 MP limit and two-upload limit are configured, but a full-size image memory test, maximum-size upload test, multi-client load test, and end-to-end video/3D benchmark were not run. A decoded 40 MP image can require multiple large working buffers.
8. **Hosted installers passed, but owner acceptance remains open:** the Windows x64, macOS Intel, and Apple Silicon matrix passed and uploaded three artifacts; hosted install/runtime smoke tests succeeded. Direct sandbox download/readback of those artifact files failed at the Azure blob TLS endpoint. Owner installation on real end-user machines, update behavior, code signing, Windows SmartScreen/Gatekeeper behavior, and macOS notarization remain unverified.
9. **Model clearance is owner-gated:** no commercially cleared background-removal checkpoint has been selected. BRIA RMBG-2.0 is non-commercial absent a separate agreement; other checkpoint rights/provenance also need review. The code gate is an operator acknowledgment only, not legal automation.
10. **Release/legal readiness:** the tracked license is proprietary All Rights Reserved; owner/legal review should ensure customer-use terms match the hosted/downloadable product. FFmpeg's actual included binary/license and a version-specific Python dependency-license/SBOM report must be reviewed from the real archive. Dormant backup modules and project-document naming inconsistencies remain repository-maintenance work.

## Lint note

The configured project Ruff check passes. A separate exhaustive `ruff check . --select ALL` diagnostic reports **7,157 findings across 119 paths**, led by `E501` line length (511), `T201` print statements (494), `ANN001` missing annotations (482), `PLR2004` magic-value findings (451), `S101` asserts (407), and `COM812` trailing commas (349). This is a broader style/annotation/security-style backlog—not the repository's configured correctness gate. Ruff also warns that docstring rules D203/D211 and D212/D213 are mutually incompatible; Ruff ignores one from each pair.

## Reproduction commands

Run from the repository root with the supplied virtual environment, or install the declared development dependencies first:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m compileall -q app tests
git diff --check
```

## Conclusion

**The local code audit and hosted desktop matrix pass; the requested installable appliance is not production-certified.** All 136 tests and configured static/compile/diff checks pass. GitHub Actions CI passed, and the Windows x64, Intel macOS x64, and Apple Silicon macOS arm64 bundles were built, smoke-tested on their native hosted runners, and uploaded. The sandbox could not independently download/read back those artifacts because the GitHub Azure blob transfer failed at TLS; no tag release was published. Owner installation/acceptance, code signing/notarization, model-license approval, canonical pricing, live billing, production, email, external-connector, and real-vehicle visual checks remain open. Those cannot be proven from this credential-free Linux checkout.
