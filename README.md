# LotVue360

**360° vehicle merchandising for independent car dealers** — an open, self-hostable alternative to GLO3D / Spyne / CarCutter / MotorCut.

Capture a car on a phone, get a studio-quality 360° spin viewer (background cut-out + turntable), interior panoramas, AI video commercials, an AI sales chatbot, lead capture, a public showroom, and one-click syndication to Facebook, Google, eBay and Craigslist — at a flat price instead of $2,400+/mo.

---

## Features

- **360° exterior spin** — frame-interpolated from 8+ photos, drag/zoom, virtual turntable + background removal (the "car cutter")
- **Interior 360 panoramas** (Pannellum) + gallery + video tab
- **AI video commercials** — WAN 2.2 image-to-video + Kokoro TTS voiceover (needs a CUDA GPU + ComfyUI)
- **AI BDC chatbot** — 24/7 vehicle-aware assistant, grounded in decoded specs
- **Lead magnets** — trade-in valuation, price advisor, history request, test-drive booking, request video
- **Virtual showroom** microsite per dealer + embeddable iframe viewer
- **VIN decode** (NHTSA), recalls, EPA economy; reverse-VIN barcode scan; Black Book / AutoCheck adapters
- **Syndication** — Facebook catalog (Comscore-ready), Google Vehicle Listings, eBay Motors API, Craigslist, Postiz, DMS import (DealerCenter / Frazer), FTP media push
- **Window sticker + FTC Buyers Guide** PDF generator
- **Mobile capture PWA** (iOS + Android), full photo management, JWT auth, Stripe billing
- **Ships three ways** — Python server, installable desktop app, and a portable `.exe` that runs on any Windows PC

## Tech stack

FastAPI (Python 3.11) · SQLite · vanilla-JS embeddable viewer · Pannellum · ComfyUI (AI video) · Kokoro TTS · FFmpeg · Stripe · JWT (python-jose) · NHTSA vPIC.

## Quick start (dev)

```bash
# Python 3.11
pip install fastapi uvicorn python-multipart Pillow opencv-python qrcode reportlab python-jose stripe pillow-heif
cd app
cp .env.example .env          # fill in your own keys
python -m uvicorn main:app --host 0.0.0.0 --port 8360
# open http://127.0.0.1:8360
```

## Configuration

All integrations activate purely via environment variables in `app/.env` (see `app/.env.example`). Anything without a credential stays **dark** and refuses with a clear message — no fake data.

## Build the portable Windows app

```bash
cd app
python -m PyInstaller --noconfirm --clean LotVue360_standalone.spec
python ../assemble_portable.py     # adds the app/ folder + native DLLs
python ../make_clean_build.py      # OPTIONAL: produce a clean install (fresh DB, no secrets)
```

Output: `dist/LotVue360_standalone/` — copy to any Windows PC, run `LotVue360.exe`.

## License

Proprietary — © ABUZ8 LLC. All rights reserved.

> Security note: never commit `.env`, `*.db`, `storage/`, or any API keys. See `.gitignore`.
