# NEXT (v1.1 and owner actions, one line each)

- Purge the removed personal photos (`app/vehicles/764948/photos/`, added in `f1c416d`) from git history; this rewrites `main` and needs a force push, so it is the owner's call.
- Move `mobile_native/lotvue-upload.keystore` out of git: back it up outside the repo first, then `git rm --cached` it (`*.keystore` is already ignored); consider a fresh upload key since it sits in history.
- Make the packaged `--self-test` run an upload → spin round trip so the installer smoke test catches core-flow crashes like the 2026-10-15 capture-gate 500.
- Align the `/api/health` version (`2.0.0`) with the installer version (`2.1.0`).
- Drop the server filesystem path (`source`) from the capture gate summary returned by `/api/upload`.
- Point the `desktop-release.yml` branch push trigger at `main` instead of the retired `arena/01a0ffa3-car-360-viewer` branch.
- Fix the invalid `\w` escape in the `validate_video.py` docstring (DeprecationWarning).
