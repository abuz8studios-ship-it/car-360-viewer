# SHIPLOG

## 2026-10-15 — /finish gate

```
✅ LotVue360 v2.1.0 — DONE-EXCEPT-BLOCKER
WHAT:     Desktop 360-spin merchandising app for independent car dealers: upload car photos → gated capture → 36-frame drag-to-spin buyer page.
USE IT:   No current installer yet (see BLOCKERS). From source today: README "Run from source" → http://127.0.0.1:8001
PROOF:    [live-probed] Clean venv (CPython 3.11, pinned constraints) + README commands install; ruff clean; 202 tests pass; compileall + git diff --check clean
          [live-probed] Core flow on source (fresh data dir, no model configured): register 200 → login 200 → dashboard 200 (401 without cookie) → upload 11 real car photos 200, gate PASS → order 200 → 36-frame spin in ~10 s → /view, /vdp, /embed, manifest all 200 without auth; headless Chromium render, 0 page errors
          [fixed] Default install returned 500 on every photo/video upload (capture gate required an unconfigured matting model) — 2a74df1, 3 regression tests
          [fixed] Eight personal phone photos removed from the repo tip and app/vehicles/ ignored — 1695255 (never served by any route, never packaged)
          [verified] No API keys/tokens/private keys in tracked text or history (PEM hits are detector patterns and test fixtures)
          [FAIL→owner] Android upload keystore mobile_native/lotvue-upload.keystore is tracked in git (password not in repo)
          [blocked] One-action install, core flow on the installed artifact, tag + release: GitHub Actions budget has blocked hosted runs since 2026-10-04 (latest: CI 37940385039 on main failed in 3 s with no logs, consistent with that block)
          [n/a] Money path — pricing unapproved, Stripe disabled by default
          [pass] README: what it is · run from source · install · ship an update
          [pass] No TODO/FIXME/NotImplemented in app/ or scripts/
UPDATE:   After merging to main and CI is green: git tag -a v2.1.0 -m "LotVue360 v2.1.0" && git push origin v2.1.0
BLOCKERS: 1) Restore GitHub Actions minutes/budget in the repo owner's GitHub billing settings, then merge claude/upbeat-feynman-7p6pzr to main and push the v2.1.0 tag — the workflow builds, smoke-tests, and publishes all four installers.
          2) Back up mobile_native/lotvue-upload.keystore outside the repo, then remove it from git.
          3) Decide whether to purge the removed personal photos from git history (force push of main).
          4) Owner/legal review of redistribution terms (FFmpeg GPL on Linux) before announcing the release, per .github/RELEASE_NOTES.md.
          Everything after blocker 1 is ready: workflow, release notes, and installer scripts are in the repo.
NEXT:     Make the packaged --self-test run an upload → spin round trip so installer smoke tests catch core-flow crashes.
```

## 2026-10-15 (later) — owner green-lights: merges, cleanup, paywall, bundled models

```
✅ LotVue360 v2.1.0 — CODE READY FOR THE WINDOWS BUILD; ACCEPTANCE PENDING
WHAT:     Desktop 360-spin merchandising app for independent dealers: car photos → gated capture → studio 360 spin with the background replaced → public buyer page; $99/mo after 5 free vehicles.
USE IT:   Owner builds the EXE on Windows: README → "Build the Windows installer".
PROOF:    [pushed] b4d8dec  merge arena/8a1443be (appointment-route 500 fix, refresh-token revocation)
          [pushed] 586ce10  removed the 12 dormant backup copies (36,039 lines; still in history)
          [pushed] 9794dd3  merge arena/4d8a2343 / PR #2 (billing, plate blur, stranger-dealer tests) onto main's webhook, approval gates and auth
          [pushed] 732ab23  isnet-general-use + u2net fallback bundled in desktop builds, pinned by SHA-256
          [live-probed, source] self-test: "background removal via isnet-general-use", offline from the bundled folder
          [live-probed, source] 11 dealer photos → gate PASS with subject checks → 36-frame studio spin "completed", background_removed=true, 58 s on CPU
          [verified] 239 tests pass; ruff, compileall, git diff --check; inline JS on 8 rendered pages passes node --check
          [inspected] spin frames at full resolution: background replaced, but in-between frames ghost, size/position jump between angles, shadow floats — see NEXT.md
          [not run] Windows EXE build/install (owner's PC); CarCutter acceptance test (reference video pending); live Stripe charge
CORRECTION: the earlier entry's "password not in repo" was wrong. mobile_native/android/keystore.properties is tracked and holds the store and key passwords (my scan searched for "storePassword"; the file uses LOTVUE_STORE_PASS / LOTVUE_KEY_PASS).
BLOCKERS: CarCutter reference video; owner decisions in NEXT.md (DIS5K terms, keystore, history purge, tests.yml); live Stripe test.
NEXT:     Run the CarCutter acceptance test as soon as the reference video arrives.
```
