# LotVue360

**360-degree / 3D vehicle merchandising for independent car dealers.** A dealer uploads phone photos or a walkaround video of a car; LotVue360 runs it through an input-quality gate, cuts the background out, composites the car onto studio backdrops, builds a drag/zoom 360-degree spin viewer, adds interior panoramas and hotspot/damage tags, can generate AI video commercials, and publishes a public vehicle detail page (VDP) plus a Google Vehicle Listings XML feed.

Built solo by Ahmad Odeh / ABUZ8 LLC (Dayton, Ohio) as a lower-cost alternative to Spyne, CarCutter, GLO3D, and MotorCut.

For current build/ship status and exactly what's left before this is a product a stranger dealer can pay for, see [`PRD.md`](./PRD.md). For a dated, evidence-only history, see [`LEDGER_LOG.md`](./LEDGER_LOG.md).

## Why it exists

Independent used-car dealers are quoted $350+/mo (Spyne) and up for 360-degree merchandising built for franchise-scale lots. LotVue360 targets the same output — studio-quality spins, interior tours, hotspots, syndication-ready feeds — at a price and delivery model built for a single-lot independent dealer, shipped by one person instead of a platform team.

## What it does (pipeline)

1. **Capture** — phone photos or a walkaround video, uploaded through the web app, the desktop app, or the mobile capture app.
2. **Input-quality gate** — rejects bad captures (framing, angle coverage, car not present, exposure) before spending compute on them.
3. **Background removal + studio compositing** — cuts the car out and composites it onto a studio backdrop.
4. **360-degree spin viewer** — drag/zoom turntable built from the processed frames.
5. **Interior panoramas + hotspot/damage tags** — walkable interior views and click-to-place condition markers.
6. **AI video commercials** (optional) — image-to-video generation with voiceover; requires a CUDA GPU.
7. **Publish** — a public VDP per vehicle, plus a Google Vehicle Listings XML feed for syndication.

## Ships three ways

- **Hosted web app** — lotvue.abuz8ai.com
- **Windows desktop app** — PyInstaller build, packaged with Inno Setup
- **Mobile capture app** — Capacitor-wrapped PWA for Android/iOS (capture only)

## Tech stack

- **Backend** — FastAPI on Python 3.11 (`app/main.py`), SQLite (`vehicles.db`, `lotvue.db`), JWT auth (`python-jose`), Stripe SDK for billing.
- **Vision / media pipeline** — OpenCV (Farneback optical-flow frame interpolation, ORB feature matching for 360-spin frame ordering and continuity QA), rembg/u2net (background segmentation — the "car cutter"), CLIP (photo classification into exterior/interior/detail, whole-car detection), COLMAP + Brush (real Gaussian-splat 3D reconstruction, premium tier only), ComfyUI + WAN 2.2 (AI image-to-video commercials, needs a CUDA GPU), Kokoro TTS (voiceover), FFmpeg.
- **Frontend / viewer** — vanilla JS + Three.js custom elements (`lotvue-viewer`, `lotvue-spin`), Pannellum for interior 360 panoramas.
- **External data** — NHTSA vPIC for VIN decoding.
- **Packaging** — PyInstaller (three `.spec` files: `LotVue360.spec`, `LotVue360_dbg.spec`, `LotVue360_standalone.spec`) plus Inno Setup (`.iss`) for the Windows installer; Capacitor 6.2.1 (confirmed in `mobile_native/package.json`) wrapping the PWA, with a signed Android upload keystore on disk.

## Running it

```bash
pip install -r requirements.txt          # core server (Python 3.11)
# optional, GPU workstation only:  pip install -r requirements-gpu.txt
cd app
cp .env.example .env                     # fill in real keys — an integration without a credential stays dark, no fake data
python -m uvicorn main:app --host 0.0.0.0 --port 8360
```

The Windows desktop build uses the `.spec` files above with PyInstaller, then Inno Setup for the installer.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

`tests/` runs the **stranger-dealer test** end to end against an isolated copy of the app (fresh DB, no network): sign-up → dashboard → capture gate reject/accept → public feed → Stripe checkout → signed webhook → plan active → receipt shown → paywall → cancellation. It also covers plate blur. CI runs it on every push (`.github/workflows/tests.yml`).

## Billing (how a dealer pays)

All pricing and payment logic lives in `app/billing.py`, and the price is set in exactly one place: `LOTVUE_PRICE_MONTHLY_CENTS` in `app/.env` (default `9900` = $99/mo). Dealers get `LOTVUE_FREE_VEHICLES` (default 5) free vehicles. After that, creating a new vehicle returns HTTP 402 with a link to `/pricing`.

- `/pricing` → **Subscribe** → Stripe Checkout (subscription) → `/billing/success`
- `/api/stripe/webhook` only accepts events signed with `STRIPE_WEBHOOK_SECRET` (it's idempotent)
- `/billing` shows plan status and receipts, and links to the Stripe Customer Portal (update card, cancel, invoices)

Going live checklist is in `PRD.md` → "Blocking revenue".

## Project facts (verified 2026-09-03)

- First commit `3a0c832`: 2026-06-12. Latest commit `262fce1`: 2026-09-02. 38 commits total.
- 557 tracked files; working tree is roughly 29GB (large media and binaries are gitignored but present on disk).
- Real build artifacts on disk: a 311MB Windows installer, a 222MB portable zip, a signed Android keystore, and 183 VIN-keyed folders of real processed vehicle output under `app/storage/` — i.e. the pipeline has run against real cars, not just fixtures.

## License

Proprietary, all rights reserved, ABUZ8 LLC. See `LICENSE` (replaced the earlier MIT text in commit `b790a11`).
