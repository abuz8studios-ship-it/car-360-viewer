# AUTO 360 — 360 Car Viewer SaaS Platform

## Status: PHASE 2-9 COMPLETE

## Features

### Core (from LOTVUE MVP)
- FastAPI backend, video upload, frame extraction
- Photo upload, background removal (rembg)
- Auto-enhance, HEIC support
- SQLite DB, 360 viewer, showroom backgrounds, hotspots
- 36-frame smooth spin, frame interpolation

### New in AUTO 360 v2.0
- **Auth**: User registration, login, session storage
- **Credits**: Free 5 credits, $2.99 single, $19.99 starter, $75/mo dealership
- **Watermark**: Preview mode (watermarked) vs Clean mode (no watermark)
- **Stripe**: Checkout sessions, webhook handler, payment records
- **GPU Detection**: Automatic GPU detection on startup
- **Health Check**: `/api/health` endpoint
- **Viewer 2.0**: Momentum physics, pinch zoom, improved touch
- **Mobile Capture**: 8-position guided capture page
- **Pricing Page**: Full pricing page with Stripe integration
- **Dealership Accounts**: Mohammed's Drive Smart Auto Sales onboarded

## Pages
- `/` — Dashboard (AUTO 360 branded)
- `/capture` — Upload video or photos
- `/capture-mobile` — Mobile-guided 8-position capture
- `/capture-guide` — How to capture tips
- `/settings` — Dealer settings
- `/view/{vin}` — 360 viewer
- `/embed/{vin}` — Embeddable iframe viewer
- `/pricing` — Pricing plans
- `/register` — User registration
- `/login` — User login
- `/health` — System health check
- `/public-url` — Sharing instructions
- `/ai-commercial` — AI commercial (ComfyUI)

## API Endpoints
- `GET /api/health` — System health, GPU info
- `POST /api/register` — Create account
- `POST /api/login` — Sign in
- `GET /api/user/{email}` — Get user info & credits
- `POST /api/set-plan` — Set user plan
- `GET /api/plans` — Get pricing plans
- `POST /api/create-checkout-session` — Stripe checkout
- `POST /api/stripe/webhook` — Stripe webhook
- `GET /api/watermark/{vin}` — Get watermark mode
- `POST /api/watermark/{vin}` — Set preview/clean
- `POST /api/upload` — Upload video
- `POST /api/upload/exterior` — Upload exterior photos
- `POST /api/upload/interior` — Upload interior photos
- `POST /api/finalize/{vin}` — Generate 360 spin
- `GET /api/vehicles` — List all vehicles
- `DELETE /api/vehicle/{vin}` — Delete vehicle
- `POST /api/hotspot` — Add hotspot
- `GET /api/hotspots/{vin}` — Get hotspots
- `DELETE /api/hotspot/{id}` — Delete hotspot
- `POST /api/dealer` — Save dealer settings
- `GET /api/dealer` — Get dealer settings
- `POST /api/showroom` — Upload showroom background
- `GET /api/showroom` — Get showroom status

## Database Tables
- `vehicles` — VIN, year, make, model, price, frames, watermark_mode, user_email
- `users` — email, password_hash, plan, credits, stripe_customer_id
- `payments` — user_id, amount, plan, credits, stripe_payment_id, status
- `credit_log` — user_id, amount, action, vin
- `hotspots` — vin, photo_filename, tag, hotspot_x, hotspot_y
- `dealers` — name, display_name, phone, email, website, brand_color, plan

## To Run
```
cd E:\ABU\360Car\app
E:\Miniconda3\envs\lotvue\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

## Stripe Setup
Set environment variables:
```
STRIPE_SECRET_KEY=sk_live_xxxxx
STRIPE_PUBLISHABLE_KEY=pk_live_xxxxx
STRIPE_WEBHOOK_SECRET=whsec_xxxxx
```

## White-Label
The same backend serves:
- AUTO 360 (current)
- Clothes/Fashion 360
- Furniture 360
- Any product 360
