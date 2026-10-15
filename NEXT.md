# NEXT (one line each; owner decisions marked)

- **Acceptance (blocked on the CarCutter reference video):** run 4 photos → 360° MP4 against it. Since 6207c4e spins are built cut-out first (constant size, tyres on one ground line, no double exposures); 4 views far apart turn by width tween, not true new angles — matching CarCutter there would need view synthesis (3D).
- **Owner decision:** `isnet-general-use` was trained on DIS5K, whose dataset terms restrict commercial use without the authors' permission; keep it as primary, get permission, or set `LOTVUE_REMBG_MODEL=u2net` (Apache-2.0).
- **Owner decision:** move `mobile_native/lotvue-upload.keystore` and `mobile_native/android/keystore.properties` (which holds the store and key passwords) out of git after backing them up; consider a new upload key.
- **Owner decision:** purge the removed personal photos (`app/vehicles/764948/photos/`, added in `f1c416d`) from git history (rewrites `main`, needs a force push).
- **Owner step:** roll the Stripe keys, register the webhook, then one live $99 charge + refund before checking PRD step 7.
- **Owner decision:** `.github/workflows/tests.yml` (from PR #2) runs the same pytest suite as `ci.yml`, doubling Actions minutes per push.
- Extend the packaged `--self-test` from "model loads and cuts out an image" to a full upload → spin round trip.
- Align the `/api/health` version (`2.0.0`) with the installer version (`2.1.0`).
- Drop the server filesystem path (`source`) from the capture-gate summary returned by `/api/upload`.
