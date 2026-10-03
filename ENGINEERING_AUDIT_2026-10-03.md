# Engineering Audit — LotVue360

**Audit date:** 2026-10-03
**Scope:** the checked-out repository, local automated tests, local source/runtime probes, and synthetic-only microbenchmarks.
**Disposition:** **Local code checks and hosted desktop smoke tests pass; this is not a production-readiness or live-integration sign-off.**

## Executive summary

The checkout now has reproducible base/dev dependency files, an automated regression suite, safer tenant/path handling, bounded upload parsing, bounded background work, and explicit local verification. The audit also found and fixed two issues that deserved fail-closed behavior:

1. **Pricing was contradictory across catalogs and Stripe routes.** Prices remain unapproved. Public price disclosure and charge paths default closed. A second guard requires the checkout and subscription amounts to agree for the public single, starter, and dealership tiers. Checkout/unlock amounts now derive from the catalog, subscription amounts are generated from the monthly catalog amount, and legacy non-public subscription tiers are rejected. The existing dealership amounts still disagree (**$75.00 vs. $99.99**), so even setting the approval flag by itself does not publish prices or permit a charge.
2. **An async CSV/DMS import path performed synchronous database-backed authentication on the event loop.** The auth lookup now runs in the worker threadpool; a thread-identity regression test covers both import routes. The active app exposes 192 route operations: 157 synchronous handlers and 35 async handlers. The async handlers were reviewed for blocking database, filesystem, network, and image-processing work; those operations are delegated to worker threads or bounded background jobs, while request-body reads remain async.

The latest local automated suite reports **175 passed in 3.48 s**. A timed subprocess run measured **4.34 s wall time** and **139.9 MiB peak child RSS**. The suite now includes 16 mocked ComfyUI scene/workflow, output-path, timeout, route-labeling, local cut-out, configuration-status, atomic-staging, and process-local concurrency regressions. Configured Ruff, Python compilation (including the PyInstaller spec), `pip check`, and `git diff --check` pass for the current edits; workflow-YAML/matrix/order validation, release-constraint resolution, and installer-shell syntax checks also passed in the prior release verification. Six Background Studio regressions continue to verify that strict jobs publish nothing unless cut-out and requested compositing succeed, preserve prior outputs on failure, and reject all-transparent/all-opaque masks. A clean-source launcher smoke test completed in **2.861 s** with a **148.9 MiB child-process peak RSS**, generated all 40 backdrops, verified 8 compatibility scenes and the local capability snapshot, and confirmed that the snapshot makes no external probes; that measurement predates the ComfyUI bridge. All performance numbers are deliberately narrow. **They are not full upload, image-processing, ComfyUI/GPU, 360-render, or Gaussian-splat pipeline measurements.**

Hosted verification for the ComfyUI bridge passed on commit `7fa13aa`: CI run **37140754753** and desktop matrix run **37140754758**. All three platform builds uploaded new Windows x64, Intel macOS x64, and Apple Silicon macOS arm64 artifacts. Before upload, native runners verified each inner ZIP's SHA-256 sidecar, platform/build manifest, archive paths, FFmpeg/FFprobe binary hashes and license notices, and exclusions for user secrets, databases, private keys, and model weights. Native smoke tests installed/launched all bundles; Windows also verified uninstall and retained user data, while both macOS jobs removed the installed app bundle after self-test. GitHub artifact metadata lists three 30-day artifacts. Direct downloads from this sandbox still fail at GitHub's Azure blob endpoint with TLS EOF/`SSL_ERROR_SYSCALL`, so uploaded ZIPs have not been independently downloaded/read back. No tag-triggered GitHub Release was published. This proves hosted packaging and smoke behavior on these runners, not a live ComfyUI/model run, external workflow validity, or owner acceptance. A later documentation-only commit `019df43` also passed CI run **37141525144**; its desktop workflow **37141525154** passed both macOS jobs but its Windows job failed during the FFmpeg download with a reported truncated transfer (`IncompleteRead`: 71,339,856 bytes received, 725,353 bytes short). The complete three-platform pass is run `37140754758` on the application-source commit `7fa13aa`; no application code changed in `019df43`.

There is no `app/.env`, no GPU/CUDA runtime, no local FFmpeg/FFprobe binary, and no vehicle output files under `app/storage/`. Production uptime, real payments, email delivery, external feeds, real image quality, owner-run end-user installation, code signing/notarization, and commercial model clearance remain unverified.

## Scope and method

- Read the repository PRD/README and treated historical workstation, production, and dealer-run notes as dated evidence—not fresh verification.
- Inventoried the registered FastAPI/OpenAPI operations and active route-handler definitions. Historical `main_*` backup modules are not counted as active route modules.
- Reviewed async route handlers and the synchronous I/O boundaries. Added regression tests where the audit found a blocking auth/database call on an async import path.
- Ran the full local pytest suite, the repository-configured Ruff check, `compileall`, and `git diff --check`; additionally parsed the GitHub Actions YAML, dry-ran both constrained pip installations, checked `pip`, and syntax-checked the macOS shell installers.
- Exercised `/pricing` and `/api/plans` through FastAPI `TestClient`, benchmarked the plate-blur function against a synthetic 640×480 image, and cold-started the source launcher after deleting its generated catalog. No remote service was contacted by those checks.
- Reviewed desktop staging filters, FFmpeg license-gate logic, the PyInstaller spec, per-user installers, and three-platform workflow. Current runs `37140754753` and `37140754758` passed CI and the Windows x64, macOS Intel, and Apple Silicon archive-verification/build/install/runtime-smoke matrix on commit `7fa13aa`, including the ComfyUI bridge source. The actual build-host ZIPs were verified before upload; no frozen platform artifact was built locally, and sandbox downloads still fail with TLS EOF at the Azure blob endpoint, so there was no independent local ZIP readback.

## Verification metrics

| Check | Result | Measurement boundary |
|---|---:|---|
| Automated tests | **175 passed** | Latest local pytest suite; integrations are mocked/isolated where appropriate |
| Test modules | **37** | `tests/test_*.py` |
| ComfyUI bridge regressions | **16 included in suite** | Workflow/API behavior is mocked; no real ComfyUI, custom-node, model, or GPU call |
| Timed pytest run | **4.34 s wall, 139.9 MiB peak child RSS** | Linux sandbox, CPython 3.11.2; measured in a subprocess (pytest reported 3.48 s) |
| Clean source-launcher smoke | **2.861 s, 148.9 MiB peak RSS** | Cold startup generated the 40 backgrounds; Python 3.11.2, Linux x86-64, 2 reported CPUs; not a frozen build |
| Configured Ruff | **Pass** | Project policy in `ruff.toml`: `ASYNC`, `E722`, `F811`, `F821`, `F823` |
| Python compile check | **Pass** | `compileall -q app tests scripts` plus `py_compile` of `app/LotVue360_standalone.spec` |
| Dependency consistency | **Pass** | `pip check` in the supplied development environment |
| Whitespace/conflict check | **Pass** | `git diff --check` |
| Workflow syntax | **Pass** | PyYAML parse; Windows x64/macOS x64/arm64 matrix and archive-verifier-before-smoke/upload ordering asserted |
| Dependency resolution | **Pass locally** | `pip install --dry-run` for dev and desktop requirements using the checked-in CPython 3.11 constraints; Linux only |
| Intel macOS wheel compatibility | **Pass** | Downloaded CPython 3.11 x86-64 wheels for cryptography 48.0.1, cffi 2.1.1, ONNX Runtime 1.23.2, llvmlite 0.45.1, Numba 0.62.1, and NumPy 2.3.5; hosted Intel build then completed |
| Hosted CI for ComfyUI bridge | **Pass, run 37140754753** | Commit `7fa13aa`; hosted lint, full test suite, and whitespace check succeeded. |
| Hosted desktop matrix for ComfyUI bridge | **3/3 pass, run 37140754758** | Commit `7fa13aa`; Windows x64, macOS x64, and macOS arm64 builds, archive verification, native install/runtime smoke, and artifact uploads succeeded. |
| GitHub artifact-store metadata | **3 artifacts uploaded (run 37140754758)** | 30-day retention through 2026-11-02 UTC. Outer artifact-wrapper sizes: Windows 324,050,050 bytes; macOS x64 286,918,595 bytes; macOS arm64 277,260,848 bytes. These are not measured inner installer ZIP sizes. |
| Independent artifact download/readback | **Not completed** | `gh run download` failed with EOF against Azure blob storage; `curl --range` failed with `SSL_ERROR_SYSCALL`. The hosted verifier checked each actual generated ZIP and native runners extracted/smoke-tested it before upload |
| Installer shell syntax | **Pass** | `bash -n install-lotvue.sh Install-LotVue360.command`; does not execute macOS system calls |
| Exhaustive Ruff diagnostic | **7,270 findings across 123 paths** | `ruff check . --select ALL`; not the configured project gate; see lint note below |
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
| ComfyUI scene-generation jobs | **1 per process** | Additional requests receive 429 + `Retry-After: 30`; process-local, so aggregate capacity multiplies with worker count |
| ComfyUI scene workflow | **1 MiB / 500 nodes** | Owner-supplied API-format JSON; required token structure validated locally |
| ComfyUI scene image files | **30 MiB input and output; 40 MP decode cap; 1920 px max edge by default** | Bounded before Comfy submission/output conversion; model/GPU memory remains installation-specific |
| ComfyUI scene timeout | **300 s default; bounded 10–1800 s by environment setting** | Includes submit/poll wall time; not a live GPU performance measurement |
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

- The repository contains per-user Windows install/uninstall and macOS install scripts, an in-process frozen launcher, a three-host GitHub Actions matrix (Windows x64, macOS Intel, macOS Apple Silicon), SHA-256 sidecars, Python 3.11 release constraints, and `scripts/verify_desktop_artifact.py` with synthetic archive-regression tests. Branch pushes run the matrix; only a `v*` tag can publish a GitHub Release.
- Current CI run **37140754753** and desktop run **37140754758** passed on commit `7fa13aa`. All three archive-verifier, native-build, smoke-test, and upload jobs succeeded. Windows x64 installed/launched the frozen runtime, checked its self-test log, uninstalled, and retained user data; macOS x64 and arm64 installed the `.app`, passed the frozen self-test, and removed the app bundle. GitHub artifact metadata reports three 30-day uploads; outer wrapper sizes are 324,050,050 bytes (Windows), 286,918,595 bytes (macOS x64), and 277,260,848 bytes (macOS arm64), expiring 2026-11-02 UTC. These are not inner installer ZIP sizes. The hosted builder verified each actual ZIP/checksum/manifest/FFmpeg notice and exclusion scan before upload, but sandbox TLS EOF still prevents independent ZIP readback. This matrix includes the ComfyUI adapter code, not a ComfyUI workflow, custom nodes, model weights, or a live scene generation. A later documentation-only push triggered run `37141525154`; macOS x64/arm64 passed again, while its Windows job failed during the remote FFmpeg download with a truncated-transfer `IncompleteRead` (71,339,856 bytes received, 725,353 bytes short). This is an external download failure on a documentation-only commit, not evidence of a Windows source/build regression; the complete Windows build/install smoke passed on source commit `7fa13aa`.
- Two Intel macOS dependency gaps were diagnosed across the hosted retries: newer `cryptography` no longer publishes Intel Mac wheels, and PyPI no longer supplies x86-64 macOS `llvmlite` wheels after 0.45.1. The latter fell back to a source build and failed because LLVM CMake files were unavailable. Constraints now use wheel-compatible `cryptography==48.0.1`, `llvmlite==0.45.1`, `numba==0.62.1`, `numpy==2.3.5`, and `onnxruntime==1.23.2` on Intel Mac. Platform-marker regression tests protect this stack while other platforms retain current pins. The follow-up native Intel runner completed dependency installation, build, archive verification, smoke test, and artifact upload.
- The archive verifier checks the actual ZIP sidecar digest, archive/platform/build identity, traversal/duplicate paths, required installers and app files, manifest exclusions, FFmpeg/FFprobe hashes and license notices, and bundled secret/database/private-key/model-weight patterns before upload. The initial hosted runs exposed false positives for Stripe source modules, ONNX Runtime sample graphs, and Certifi's public CA bundle; source code remains allowed, ONNX Runtime's test-only `datasets` are excluded by the PyInstaller spec, and PEM files are rejected only when named as private or containing private-key PEM markers. The final 3-platform run passed these checks. Direct artifact downloads still fail with Azure blob TLS EOF/`SSL_ERROR_SYSCALL`, so I have not independently read back the ZIPs. Artifact-store metadata confirms all three 30-day uploads; listed sizes are transport-wrapper sizes, not inner ZIP sizes. The tag-only publish job was skipped; no GitHub Release was created.
- The builder's FFmpeg stage ran on each host, refuses nonfree or unidentified builds, writes the detected license and `-version`/`-buildconf` notices, and records binary SHA-256 values. The source staging filter excludes env variants, secret/credential files, databases/logs, user storage, and model-weight extensions while preserving `.env.example`. Local checks include builder filter tests, a PyInstaller dependency-data probe (the only model-like package data was three ONNX Runtime sample graphs removed by the spec), workflow YAML/matrix assertions, Linux dependency-resolution dry runs, Intel wheel downloads, installer shell syntax, and the **source** launcher self-test. That source test generated/verified 40 backdrops and 8 compatibility scenes and confirmed no external probes. The sandbox itself has no FFmpeg/FFprobe executable; hosted builds are the binary/license-gate evidence.

- `rembg` inference now fails closed unless `LOTVUE_REMBG_MODEL` is explicitly set and `LOTVUE_REMBG_LICENSE_ACCEPTED=1` is explicitly acknowledged. The first-run template leaves both off; active paths use the adapter; old backup modules with direct imports are excluded from release staging. Four tests cover the gate and a 503 response with temporary-file cleanup. The acknowledgment is not a license check, no model weights are bundled, and no model's commercial terms or inference quality are approved/tested.
- Strict Background Studio jobs now pass a required-removal policy into the shared `PhotoProcessor`: empty/all-opaque masks, missing/failed segmentation, missing requested scenes, or failed compositing return an error without publishing source photos or substituting another scene. The job publishes staged results only when work succeeded and retains previous studio outputs when every photo fails. Six new regression tests cover cut-out success, both mask extremes, strict-policy queue forwarding, required-scene failure, missing-cutout failure, and previous-output preservation. General Photo Studio's explicitly degraded fallback behavior is unchanged.
- Before customer-facing distribution, the owner should independently download and inspect the uploaded archives, review the build manifest/FFmpeg notices, generate/review a version-specific dependency-license or SBOM report, and test install/update/removal on representative end-user Windows/macOS machines. Code signing, Windows trust reputation, and macOS notarization remain owner-provided and are not part of this workflow.

### 4. Upload, memory, and background admission — bounded; production load remains unmeasured

- Request bodies and multipart file/field counts are checked before Starlette route parsing; ordinary requests default to a 2 MiB ceiling.
- Large video uploads are streamed to a temporary file and atomically renamed; image batches are read/processed one file at a time and capped by per-file, batch, and pixel limits.
- Large/file-upload routes are now limited to two concurrent requests per process by default; an overload returns 503 plus a 30-second retry hint. A middleware regression test proves rejection under saturation and verifies that the slot is released afterwards.
- The processing executor admits at most four jobs with two worker threads by default, deduplicates identical per-key jobs, and returns retryable busy/full responses rather than accumulating unbounded background submissions.

These controls reduce unbounded per-request and background work. They do not replace reverse-proxy timeouts, process/container memory limits, storage quotas, production monitoring, or a concurrent-load test.

### 5. Public media/viewer behavior — locally covered; no real output to inspect

Vehicle IDs, active-vehicle state, tenant ownership, path containment, symlink boundaries, filename allowlists, image size and frame caps, and public video URLs have targeted tests in the suite. Frame enumeration uses bounded `heapq.nsmallest` limits. No VIN output exists in this checkout, so there was no real spin set to render, compare, or visually inspect.

### 6. Owner-configured ComfyUI scene bridge — locally tested; no live workflow proof

- The former `/api/photo/composite` ComfyUI stub no longer reports a successful server probe as AI scene generation. The existing route now accepts `engine="local"` (the deterministic default) or explicit `engine="comfyui"`; AI results are labeled `ai_generated=true` and `review_required=true`, and their `_comfyui` output suffix keeps them separate from deterministic local composites. A missing/invalid workflow or execution failure returns an error and never falls back to the local compositor.
- ComfyUI execution requires the owner's API-format workflow path in `LOTVUE_COMFYUI_SCENE_WORKFLOW`. A bounded structural validator accepts at most a 1 MiB / 500-node graph and checks required input-image, positive-prompt, and output-prefix tokens plus an optional negative-prompt token. The bridge reuses the app's existing ComfyUI input staging and submit/poll helpers; output must resolve under the configured ComfyUI output folder, be exactly one image, and remain within 30 MiB / the configured image-pixel cap. Temporary files are removed on success/failure and the vehicle-facing output is atomically replaced.
- ComfyUI API JSON responses and submitted prompt graphs are bounded; polling uses a monotonic overall timeout. Output path traversal, unsafe filenames, multiple results, and output into ComfyUI's managed input/output folders are rejected. One scene-generation request is admitted per application process; excess requests get `429 Retry-After: 30`. These in-process limits multiply with worker count and do not establish a machine-wide GPU limit.
- The deterministic local `/api/photo/composite` operation now rejects fully opaque inputs: it needs a transparent vehicle cut-out. The existing shared PhotoProcessor route is still the integrated cut-out-plus-packaged-scene path when the operator has separately enabled a commercially cleared background-removal model. This avoids labeling an opaque photo pasted over a backdrop as a successful replacement.
- The configuration-only Dealer Studio readiness snapshot reports whether the owner workflow file passes structural checks and exposes the ComfyUI scene-job cap. It does not test endpoint reachability, custom nodes, model availability/licensing, GPU throughput, or generated-image quality. The bridge is API-only; no Dealer Studio scene-generation button has been added.
- Fifteen new tests mock workflow submission/status, output-path safety, token substitution, cleanup, fail-closed behavior, single-output/timeout enforcement, route selection/labels, local cut-out rejection, readiness reporting, and the process-local concurrency gate. They do not prove a real ComfyUI graph/model runs, that generated vehicles remain faithful, or any visual quality/performance claim. There is no ComfyUI service/GPU configured in this sandbox and no workflow or model is bundled.

## Remaining risks and owner-gated work

1. **Pricing decision and live billing:** the owner must approve one canonical offer, reconcile every checkout/subscription display and amount, configure a valid Stripe key/webhook secret, then execute a controlled test transaction and verify its receipt, webhook, idempotency, and entitlement. No live Stripe transaction was made.
2. **Production state:** no production host was accessed. Current uptime, deployment config, restart behavior, and recovery from the previously recorded outage are unverified.
3. **Outbound email:** SMTP credentials are absent here; lead-alert delivery and credentials were not tested live.
4. **External integrations:** Google feed validation against live dealer inventory, NHTSA/network services, DMS credentials, ComfyUI/Ollama endpoints, and social/listing integrations were not live-tested in this audit.
5. **Real-capture quality:** `app/storage/` has **0 files** and `app/.env` is absent. Real-dealer photo quality, output latency, failure rate, and 360 visual quality remain unmeasured; historical workstation runs are not current checkout evidence.
6. **License-plate privacy:** only a synthetic blur smoke test passed. There is no labeled real-dealer dataset or measured recall/false-negative rate, and standard-path coverage is not proven. Do not advertise guaranteed plate anonymization.
7. **High-resolution/concurrent resource use:** the 40 MP limit and two-upload limit are configured, but a full-size image memory test, maximum-size upload test, multi-client load test, and end-to-end video/3D benchmark were not run. A decoded 40 MP image can require multiple large working buffers.
8. **Hosted installers passed, but owner acceptance remains open:** the Windows x64, macOS Intel, and Apple Silicon matrix passed archive verification, installed-runtime smoke tests, and uploaded three artifacts. Direct sandbox download/readback of those files still fails at the Azure blob TLS endpoint. Owner installation on real end-user machines, update behavior, code signing, Windows SmartScreen/Gatekeeper behavior, and macOS notarization remain unverified.
9. **Model clearance is owner-gated:** no commercially cleared background-removal checkpoint has been selected. BRIA RMBG-2.0 is non-commercial absent a separate agreement; other checkpoint rights/provenance also need review. The code gate is an operator acknowledgment only, not legal automation.
10. **Release/legal readiness:** the tracked license is proprietary All Rights Reserved; owner/legal review should ensure customer-use terms match the hosted/downloadable product. FFmpeg's actual included binary/license and a version-specific Python dependency-license/SBOM report must be reviewed from the real archive. Dormant backup modules and project-document naming inconsistencies remain repository-maintenance work.

## Lint note

The configured project Ruff check passes. A separate exhaustive `ruff check . --select ALL` diagnostic reports **7,270 findings across 123 paths**, led by `E501` line length (514), `T201` print statements (496), `ANN001` missing annotations (482), `PLR2004` magic-value findings (452), `S101` asserts (421), and `COM812` trailing commas (351). This is a broader style/annotation/security-style backlog—not the repository's configured correctness gate. Ruff also warns that docstring rules D203/D211 and D212/D213 are mutually incompatible; Ruff ignores one from each pair.

## Reproduction commands

Run from the repository root with the supplied virtual environment, or install the declared development dependencies first:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m compileall -q app tests
git diff --check
```

## Conclusion

**The local code audit and hosted build/install smoke matrix pass; the product is not production-certified.** All 175 local tests and configured Ruff, compilation, dependency, and diff checks pass. Commit `7fa13aa` passed hosted CI run **37140754753** and the three-platform Windows x64 / Intel macOS / Apple Silicon matrix **37140754758**, which verified and uploaded fresh artifacts containing the ComfyUI bridge source. No ComfyUI graph, model, custom nodes, external credentials, or actual generated image were included or exercised. A later docs-only workflow attempt passed CI and both macOS jobs but hit an incomplete remote FFmpeg download in the Windows build; the complete source-commit matrix above passed all three platforms. Prior uploaded ZIPs were verified on their build hosts but could not be independently downloaded/read back from this sandbox because the GitHub Azure blob transfer failed at TLS. No tag release was published. Owner installation/acceptance, code signing/notarization, model-license approval, canonical pricing, live billing, production, email, external-connector, live ComfyUI execution, and real-vehicle visual checks remain open. Those cannot be proven from this Linux checkout without the owner's services, models, approvals, and test data.
