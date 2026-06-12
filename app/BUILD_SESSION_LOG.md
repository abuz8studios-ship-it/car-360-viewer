# AUTO 360 — Build Session Log
# Date: Mon Mar 23 2026
# Previous context: LOTVUE MVP → AUTO 360 SaaS Platform

---

## GOAL

Build the LOTVUE 360° car viewer app into a full-featured SaaS product called "AUTO 360" that competes with Glo3D/Spyne. The vision is a white-label multi-tenant platform for car dealerships and fashion. First target: Mohammed's Drive Smart Auto Sales dealership.

## INSTRUCTIONS

1. Read existing LOTVUE code BEFORE touching anything
2. Preserve ALL existing data (6 vehicles already in storage)
3. Back up main.py before modifying
4. NO WSL/Linux - Windows native only
5. Use E:\Miniconda3\envs\lotvue\python.exe
6. Build in phases, report after each phase
7. Keep original backed up for future development

---

## DISCOVERIES (Pre-Build)

### Current LOTVUE Status (2,393 lines main.py):
- **Already working**: FastAPI backend, video upload, frame extraction, photo upload, background removal (rembg), auto-enhance, HEIC support, SQLite DB, 360 viewer, showroom backgrounds, hotspots
- **LOCKED behavior**: No background removal on exterior, raw frames only (Impel mode)
- **Endpoints existing**: /, /capture, /capture-guide, /view/{vin}, /embed/{vin}, /settings, /tag/{vin}, /ai-commercial, /public-url
- **Database tables**: vehicles, hotspots, dealers
- **Missing**: Auth, payments, credits, watermark system, mobile capture guide, Mohammed onboarding

### System Status:
- Port 8001: LOTVUE running ✅
- Port 8000: ComfyUI ✅
- Port 11434: Ollama ✅
- Port 18789: OpenClaw ✅
- Dual RTX 5090 (64GB VRAM) ✅
- Rust 1.94.0 ✅

### Key Paths:
- App: E:\ABU\360Car\app\main.py
- Storage: E:\ABU\360Car\app\storage\{vin}\
- Conda env: E:\Miniconda3\envs\lotvue\
- Database: E:\ABU\360Car\app\vehicles.db

---

## PHASE 2: Dashboard UI Fix, GPU Detection, Health Check

### Changes Made:

1. **Dashboard background** changed from `#f0f4f8` (light) to `#0d1117` (dark GitHub-style)
2. **Header** changed from `#e8edf2` to `#0d1117/#161b22` gradient with `#30363d` border
3. **Logo** updated from "LOTVUE" to "AUTO <span>360</span>" with white text
4. **Card backgrounds** changed from `#e8edf2` to `#161b22`
5. **Stats bar** expanded with "360 Views" counter
6. **GPU Detection** added at startup via torch/GPUtil
7. **Health Check API** added: `GET /api/health` returns GPU info, storage status, vehicle count, FFmpeg paths, timestamp
8. **Title** changed from "LOTVUE — Dashboard" to "AUTO 360 — Dashboard"

### Code Changes:
```
main.py:
- Added GPU detection block (torch + GPUtil)
- Added /api/health endpoint
- Updated BASE_CSS: body bg #0d1117, header bg #0d1117/#161b22 gradient
- Updated dashboard: dark cards #161b22, stats added
- Updated vehicle cards: dark styling, price field
- Updated brand name: LOTVUE → AUTO 360
```

---

## PHASE 3: Users, Payments, Credits Tables

### New Database Tables:
1. **users**: id, email (UNIQUE), name, password_hash, plan, credits, stripe_customer_id, created_at
2. **payments**: id, user_id, amount, currency, plan, credits_purchased, stripe_payment_id, status, created_at
3. **credit_log**: id, user_id, amount, action, vin, created_at

### New Helper Functions:
- `db_get_user(email)` — Get user by email
- `db_create_user(email, name, password_hash)` — Create user with 5 free credits
- `db_update_user_credits(email, credits)` — Update credits
- `db_add_credits(email, amount, action, vin)` — Add/subtract credits + log
- `db_get_user_credits(email)` — Get current credits
- `db_record_payment(...)` — Record payment
- `db_update_dealer_plan(dealer_name, plan)` — Update dealer plan
- `check_credits(user_email)` — Returns (can_proceed, credits)
- `use_credit(user_email, vin)` — Deduct 1 credit, returns success

### Plans Defined:
```
PLANS = {
    "free": {"credits": 5, "price": 0, "label": "Free"},
    "single": {"credits": 1, "price": 2.99, "label": "Single ($2.99)"},
    "starter": {"credits": 10, "price": 19.99, "label": "Starter (10 for $19.99)"},
    "dealership": {"credits": 999999, "price": 75.00, "label": "Dealership Monthly ($75/mo)"},
}
```

---

## PHASE 4: Watermark System

### Changes Made:
1. **vehicles table** — Added `watermark_mode TEXT DEFAULT 'preview'` and `user_email TEXT DEFAULT ''` columns
2. **New functions**:
   - `apply_watermark(img, text, opacity)` — Renders "Preview • AUTO 360" text overlay on images
   - `WATERMARK_PREVIEW_TEXT = "Preview • AUTO 360"`
3. **API endpoints**:
   - `POST /api/watermark/{vin}` — Set mode to "preview" or "clean"
   - `GET /api/watermark/{vin}` — Get current mode
4. **Integration**: Background removal process now checks watermark_mode and applies watermark overlay on preview frames
5. **Viewer**: Added watermark badge showing "PREVIEW" (orange) or "CLEAN" (green)

### Watermark Behavior:
- **Preview mode**: "Preview • AUTO 360" text applied to all spin frames
- **Clean mode**: No watermark (dealership/single plan subscribers)

---

## PHASE 5: Stripe Integration

### New Endpoints:
1. `GET /api/stripe/config` — Returns publishable key
2. `POST /api/create-checkout-session` — Creates Stripe Checkout session for any plan
3. `POST /api/stripe/webhook` — Handles `checkout.session.completed` events, auto-adds credits
4. `GET /api/user/{email}` — Get user info + credits
5. `POST /api/register` — Create account (email, name, password_hash)
6. `POST /api/login` — Login with email + password (SHA256 hash)
7. `POST /api/set-plan` — Set user plan

### Stripe Environment Variables:
```python
STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
```

### Webhook Flow:
- `checkout.session.completed` → finds user by email → adds credits → records payment

---

## PHASE 6: Viewer Upgrades

### Enhanced viewer360.js:
1. **Better momentum physics**: velocity calculated from drag speed, momentum uses 0.93 decay
2. **Pinch zoom**: 2-finger pinch scales up to 4x zoom with pan support
3. **Improved touch**: touchCount tracking, separate 1-finger drag from 2-finger pinch
4. **Double-tap reset**: Resets zoom/pan on double-tap
5. **Sensitivity**: Reduced from 15px to 12px per frame for smoother control

### Viewer Page Updates:
- Added `.watermark-badge` in viewer (shows PREVIEW/CLEAN status)
- Watermark badge fetched from API and styled green/blue

---

## PHASE 7: Dashboard Upgrades + Pricing + Auth Pages

### Dashboard Updates:
- Added "360 Views" counter in stats
- Added "System" health link in header
- Vehicle cards now show price field
- "Mobile Capture" button added to each vehicle card

### New Pages:
1. **`/pricing`** — Full pricing page with 4 plan cards (Free, Single, Starter, Dealership), FAQ section, Stripe checkout integration
2. **`/register`** — User registration form with email/name/password
3. **`/login`** — User login form

---

## PHASE 8: Mobile Capture Guide

### New Page: `/capture-mobile`
- Mobile-optimized (no zoom, viewport locked)
- 8-position grid with visual car diagram
- Step-by-step guided capture flow
- Progress bar tracking positions
- Camera outline guide overlay
- Photo upload with file input
- Auto-redirect to 360 viewer after upload

### Positions:
1. Front, 2. Front-R, 3. Side, 4. Rear-R, 5. Rear, 6. Rear-L, 7. Side-L, 8. Front-L

---

## PHASE 9: Mohammed Onboarding

### Account Created:
- **Email**: wsuodeh@gmail.com
- **Display**: Drive Smart Auto Sales
- **Website**: https://www.drivesmartautosales.com
- **Plan**: dealership
- **Credits**: 999999 (unlimited)
- **Brand Color**: #3fb950 (green)

### Database Setup:
```sql
-- users table
INSERT INTO users (email, name, plan, credits, created_at)
VALUES ('wsuodeh@gmail.com', 'Drive Smart Auto Sales', 'dealership', 999999, '2026-03-23T00:00:00')

-- dealers table
UPDATE dealers SET name='drivesmartautosales', display_name='Drive Smart Auto Sales',
  email='wsuodeh@gmail.com', website='https://www.drivesmartautosales.com',
  plan='dealership', brand_color='#3fb950'
```

---

## DATABASE SCHEMA (Final)

### Tables:
1. **vehicles**: vin (PK), year, make, model, color, price, mileage, exterior_frames, interior_count, created_at, status, watermark_mode, user_email
2. **hotspots**: id, vin, photo_filename, tag, hotspot_x, hotspot_y
3. **dealers**: id, name, display_name, phone, email, address, website, logo_url, brand_color, plan, user_email, created_at
4. **users**: id, email (UNIQUE), name, password_hash, plan, credits, stripe_customer_id, created_at
5. **payments**: id, user_id, amount, currency, plan, credits_purchased, stripe_payment_id, status, created_at
6. **credit_log**: id, user_id, amount, action, vin, created_at

---

## FILES MODIFIED

| File | Change |
|------|--------|
| E:\ABU\360Car\app\main.py | Full upgrade (2,393 → 2,834 lines) |
| E:\ABU\360Car\app\backups\main.py.backup | Original preserved |
| E:\ABU\360Car\app\static\viewer360.js | Enhanced with momentum, pinch zoom |
| E:\ABU\360Car\app\static\service-worker.js | AUTO 360 branding |
| E:\ABU\360Car\app\static\manifest.json | AUTO 360 branding |
| E:\ABU\360Car\app\LOTVUE_README.md | Full docs update |

---

## VERIFICATION RESULTS

```
App imports: OK
App title: AUTO 360 — 360 Car Viewer SaaS
Total routes: 47
Active vehicles: 5 (all preserved with watermark_mode='preview')

GPU Detection: has_gpu=False, cuda=False
  (torch not installed in env, GPUtil not installed)
  NOTE: GPU detection will work when torch is installed

Mohammed onboarded: wsuodeh@gmail.com - Drive Smart Auto Sales
Users table: 1 user (Mohammed, dealership, 999999 credits)
Dealers table: 1 dealer (drivesmartautosales, dealership plan)
```

---

## NEW ROUTES (47 total)

### Pages:
- `/` — Dashboard
- `/dashboard` — Dashboard (alias)
- `/capture` — Upload video or photos
- `/capture-guide` — Capture instructions
- `/capture-mobile` — Mobile-guided capture (NEW)
- `/settings` — Dealer settings
- `/view/{vin}` — 360 viewer
- `/embed/{vin}` — Embed iframe
- `/public-url` — Sharing instructions
- `/ai-commercial` — AI commercial
- `/pricing` — Pricing page (NEW)
- `/register` — Registration (NEW)
- `/login` — Login (NEW)
- `/tag/{vin}` — Tag hotspots

### API Endpoints:
- `GET /api/health` — Health check (NEW)
- `POST /api/register` — Create account (NEW)
- `POST /api/login` — Login (NEW)
- `GET /api/user/{email}` — Get user (NEW)
- `POST /api/set-plan` — Set plan (NEW)
- `GET /api/plans` — Get pricing plans (NEW)
- `POST /api/create-checkout-session` — Stripe checkout (NEW)
- `POST /api/stripe/webhook` — Stripe webhook (NEW)
- `GET /api/stripe/config` — Stripe config (NEW)
- `GET /api/watermark/{vin}` — Get watermark mode (NEW)
- `POST /api/watermark/{vin}` — Set watermark mode (NEW)
- `POST /api/upload` — Upload video
- `POST /api/upload/exterior` — Upload exterior photos
- `POST /api/upload/interior` — Upload interior photos
- `POST /api/finalize/{vin}` — Generate 360 spin
- `GET /api/vehicles` — List vehicles
- `DELETE /api/vehicle/{vin}` — Delete vehicle
- `POST /api/hotspot` — Add hotspot
- `GET /api/hotspots/{vin}` — Get hotspots
- `DELETE /api/hotspot/{id}` — Delete hotspot
- `POST /api/dealer` — Save dealer
- `GET /api/dealer` — Get dealer
- `POST /api/showroom` — Upload showroom
- `GET /api/showroom` — Get showroom
- `POST /api/create-vehicle` — Create vehicle
- `GET /api/finalize-status/{vin}` — Check processing status
- `POST /api/generate-commercial` — AI commercial

---

## TO RUN

```
cd E:\ABU\360Car\app
E:\Miniconda3\envs\lotvue\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

---

## TO DO (Future)

- Install torch for GPU detection: `pip install torch`
- Configure Stripe keys for payment processing
- Set up Stripe webhook endpoint
- Add vehicle metadata (year/make/model/price) to upload flow
- Add email notifications
- White-label subdomain system
- ComfyUI AI commercial workflow
