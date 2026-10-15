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
