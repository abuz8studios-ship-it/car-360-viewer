# LotVue360 — Exact GLO3D Clone Build Specification

**For: Manus AI**
**From: Ahmad (ABUZ8) via Al Muhandis Qadir**
**Date: March 26, 2026**
**Objective: Build LotVue360 as an exact functional clone of GLO3D's Digital Merchandising product, then exceed it.**

---

## CRITICAL CONTEXT FOR MANUS

The existing LOTVUE codebase has a working exterior 360 spin viewer (frame-by-frame drag rotation with hotspots) and background removal via `rembg`. Everything below builds ON TOP of that foundation. Do NOT rebuild what already works. Assess existing code first.

---

## PART 1: WHAT GLO3D ACTUALLY IS (Corrected Analysis)

Manus's gap analysis was ~70% correct but missed critical revenue-generating features and got the architecture wrong in places. Here's what GLO3D actually delivers:

### GLO3D is NOT just a viewer — it's a 7-product platform:

1. **Digital Merchandising** (the viewer + capture pipeline) — $395/mo
2. **AI Employee / BDC** (chatbots, lead follow-up) — $944/mo  
3. **Video Marketing** (walkaround videos, feature videos, YouTube reviews) — $247/mo
4. **Social Media Marketing** (inventory-to-social automation) — $198/mo
5. **Paid Ads Management** (Google, Meta, TikTok, Craigslist auto-post) — $497/mo
6. **Local SEO** (Google Vehicle Listings, Virtual Showroom microsites) — $148/mo
7. **Lead Magnets** (Trade-in value tool, Price Advisor, CARFAX request, Book Test Drive, Request Video) — bundled

**For LotVue360 MVP, we are cloning Product #1 (Digital Merchandising) ONLY.** This is the $395/mo product. Everything else is Phase 5+.

### What Digital Merchandising Actually Includes:

| Sub-Product | What It Does |
|---|---|
| **Guided Photography** | Mobile app with step-by-step instructions + overlay outlines showing where to stand for each shot |
| **Custom Background** | AI background removal + library of ~40 professional backdrops (showroom, outdoor scenic, studio white, blur, custom upload) |
| **360 Car Photography** | Exterior 360 spin (frame-based drag rotation) + Interior 360 panorama (equirectangular via 360 camera) + hotspots + condition reports |
| **Walkaround Video** | AI-generated video tour from the captured photos (auto-narrated with vehicle specs) |
| **Engine Video** | Short video of the engine bay, auto-enhanced |
| **Web Capture** | Browser-based capture tool (no app required) for dealers who don't want to install an app |

---

## PART 2: WHERE MANUS'S ANALYSIS WAS WRONG OR INCOMPLETE

### Corrections to the Gap Analysis:

1. **"Interior View = Basic frame-by-frame spin"** — WRONG for GLO3D's interior. GLO3D uses TRUE equirectangular spherical panoramas captured with a 360 camera (Ricoh Theta, Insta360). This is NOT the same as frame-by-frame. LotVue currently has NO interior view at all. Manus correctly identified Pannellum as the solution — that part is right.

2. **Missing: Guided Photography Flow** — Manus completely ignored this. GLO3D's biggest UX differentiator is the capture wizard. It shows a car outline overlay on the phone camera, tells you "Stand here, take photo 1 of 36", and guides the user around the car. Without this, dealers produce garbage photos. LotVue needs this in the upload/capture flow.

3. **Missing: Condition Report / Damage Report** — GLO3D lets you place hotspots that say "scratch here", "dent here", "tear on seat". This is a SELLING feature for used car dealers. It builds buyer trust. Manus didn't mention this.

4. **Missing: Auto-Alignment & Auto-Scaling** — GLO3D's AI pipeline automatically aligns all 36 exterior frames so the car doesn't jump around during spin. It also auto-scales so the car is the same size in every frame. LotVue's current spin looks janky without this.

5. **Missing: Export Formats** — GLO3D exports as: embeddable iframe, GIF, MP4 video, single frame download, multi-frame download, QR code link, direct URL. LotVue currently only has iframe embed.

6. **Missing: Virtual Turntable** — Manus mentioned it but undervalued it. The turntable is a circular shadow/reflection graphic composited UNDER the car after background removal. It makes the car look like it's in a photography studio. This is a $0-cost visual upgrade that makes the product look 10x more professional.

7. **Pricing was wrong** — Manus said "$149 to $899+". Actual GLO3D pricing per Software Finder: Digital Merchandising alone is $395/mo. The full platform with all 7 products is $2,000+/mo. LotVue360 should target $99-199/mo to UNDERCUT them.

8. **Missing: VIN Decoder Integration** — GLO3D auto-populates vehicle specs (year, make, model, trim, engine, transmission) from the VIN. This powers the feature list sidebar in the viewer and the auto-generated video narration.

9. **Missing: DMS/IMS Integration** — GLO3D auto-syncs to HomeNet, vAuto, DealerSocket, etc. This is what makes dealers sticky. For MVP, we just need CSV import and manual entry. But the data model must support this.

10. **Missing: Facebook Marketplace Auto-Post** — This is GLO3D's #1 lead generation feature. It auto-posts every vehicle to FBMP. This is a Phase 3 feature for LotVue but the data model needs to support it from Day 1.

---

## PART 3: THE EXACT BUILD SPEC (What to Build, In Order)

### Phase 2A: The Unified Viewer Widget (PRIORITY 1 — Do This First)

**Goal: Transform `/view/{vin}` from a single-purpose spin viewer into a multi-tab Digital Showroom Widget identical to GLO3D's embedded viewer.**

#### 2A.1 — Tab Bar Navigation (Bottom of Viewer)

```
┌──────────────────────────────────────────────┐
│                                              │
│          [Active View Area]                  │
│                                              │
│    (exterior spin / interior pano /          │
│     gallery / video / lead form)             │
│                                              │
├──────────────────────────────────────────────┤
│  🔄 360  │  🏠 Interior  │  📷 Gallery  │  🎬 Video  │  📋 Inquiry  │
└──────────────────────────────────────────────┘
```

**Technical Requirements:**
- Bottom tab bar, fixed position within the viewer container
- Icons + text labels on each tab
- Active tab highlighted with brand color (LotVue blue)
- Tabs are configurable per vehicle (if no interior photos uploaded, hide Interior tab)
- State managed in vanilla JS (no React needed for embed widget — keep it lightweight)
- Entire viewer must work as a single embeddable `<iframe>` snippet
- Responsive: tabs stack as icons-only on mobile (<480px)

#### 2A.2 — Exterior 360 Tab (Already Exists — Polish It)

- Keep existing frame-by-frame drag rotation
- ADD: Virtual turntable graphic (circular gradient shadow) composited below the car
- ADD: Auto-play on hover (slow rotation) with play/pause button
- ADD: Fullscreen button (top-right corner)
- ADD: Zoom (mouse wheel / pinch) with multi-level (2x, 4x)
- ADD: Frame counter "12/36" small text bottom-left
- KEEP: Existing hotspot system

#### 2A.3 — Interior Panorama Tab (NEW — Pannellum)

**Integration:**
```html
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/pannellum@2.5.6/build/pannellum.css"/>
<script src="https://cdn.jsdelivr.net/npm/pannellum@2.5.6/build/pannellum.js"></script>
```

**Requirements:**
- Accept equirectangular JPEG/PNG uploads (from Ricoh Theta, Insta360, or any 360 camera)
- Also accept standard wide-angle interior photos as a fallback (display as zoomable image, not panorama)
- Pannellum viewer with: autoRotate, compass, hotspots for interior features
- Multiple scenes supported (front seat view, rear seat view, trunk view) with scene selector dropdown
- Default pitch/yaw set to driver's perspective
- hfov (horizontal field of view) default: 120°

**Upload Flow Update:**
- In the vehicle upload wizard, add "Interior Photos" section
- Accept: equirectangular images (2:1 aspect ratio auto-detected) → route to Pannellum
- Accept: standard photos (non-equirectangular) → route to Gallery tab instead
- File naming convention: `{vin}_interior_01.jpg`, `{vin}_interior_02.jpg`

#### 2A.4 — Gallery Tab (NEW)

- Thumbnail grid (3 columns desktop, 2 columns mobile)
- Click thumbnail → lightbox overlay with left/right navigation
- Swipe support on mobile
- Categories: Exterior Details, Interior Details, Engine Bay, Trunk, Damage/Condition
- Upload accepts bulk photos with drag-and-drop
- Photos auto-sorted by upload order, drag-to-reorder in dashboard

#### 2A.5 — Video Tab (NEW)

- Accept MP4 upload OR YouTube/Vimeo URL
- Embedded HTML5 video player (MP4) or iframe (YouTube/Vimeo)
- Auto-generated thumbnail from first frame
- If no video uploaded, tab is hidden
- Future: AI-generated walkaround video from the 360 frames (Phase 3)

#### 2A.6 — Inquiry / Lead Capture Tab (NEW)

**This is the MONEY tab. This is what turns a free viewer into a $99/mo SaaS product.**

Form fields:
- Full Name (required)
- Email (required)
- Phone (required)
- Message / "What would you like to know about this vehicle?" (optional textarea)
- "Request a Video Walkaround" checkbox
- "Schedule a Test Drive" checkbox
- Submit button

**Backend:**
- `POST /api/leads` endpoint
- Store in `leads` table: `id, vin, dealer_id, name, email, phone, message, wants_video, wants_test_drive, created_at, status`
- Status values: `new`, `contacted`, `converted`, `lost`
- On submit: send email notification to dealer (configurable email in dashboard settings)
- On submit: send SMS notification to dealer (Twilio, Phase 3)
- Dashboard: Leads page with table view, filter by VIN, filter by status, export CSV

#### 2A.7 — Dealer Branding Overlay

- Dealer logo displayed in top-left corner of viewer (semi-transparent, 120px max width)
- Logo uploaded in dealer dashboard settings
- Configurable: show/hide via dashboard toggle
- "Powered by LotVue360" watermark in bottom-right (small, unobtrusive) for free tier

---

### Phase 2B: Upload Pipeline Enhancements (PRIORITY 2)

#### 2B.1 — Virtual Turntable Compositing

- After `rembg` background removal, composite a turntable graphic UNDER the car
- Turntable = circular gradient (elliptical, dark center fading to transparent)
- Auto-sized to match the car's width in each frame
- Turntable graphic options in dashboard: Studio Shadow (default), Concrete Floor, Glossy Black, None
- This runs server-side during the upload processing pipeline

#### 2B.2 — Background Library

- 40+ professional backgrounds (download a free stock set or generate with AI)
- Categories: Showroom, Outdoor, Studio, Seasonal/Promotional
- Dealer can upload custom backgrounds (dealership photo, branded graphic)
- Background applied during processing after `rembg` removal
- Preview: show 3 background options side-by-side before finalizing

#### 2B.3 — Auto-Alignment (Critical for Professional Output)

- After all frames uploaded, run alignment pipeline:
  1. Detect car bounding box in each frame (YOLO or simple contour detection post-rembg)
  2. Calculate center of mass for each frame
  3. Shift each frame so car center is consistent across all 36 frames
  4. Scale each frame so car width is consistent
- This eliminates the "jumpy" spin effect from hand-held capture
- Output: aligned frames replace originals in the vehicle's frame directory

#### 2B.4 — VIN Decoder

- On VIN entry, auto-populate: Year, Make, Model, Trim, Engine, Transmission, Drivetrain, Fuel Type, Body Style
- Use NHTSA vPIC API (free): `https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json`
- Store decoded data in `vehicles` table
- Display in viewer: Feature sidebar on desktop (right side, 250px wide), or expandable drawer on mobile
- This data also powers the embed metadata (og:title, og:description for social sharing)

---

### Phase 2C: Export & Sharing (PRIORITY 3)

#### 2C.1 — Export Formats

| Format | How |
|---|---|
| **Embed Code** | `<iframe>` snippet with customizable width/height — already exists, polish it |
| **Direct URL** | `https://lotvue360.com/view/{vin}` — already exists |
| **QR Code** | Generate QR code PNG pointing to the direct URL (use `qrcode` Python library) |
| **GIF Export** | Server-side: stitch all 36 frames into animated GIF (Pillow) |
| **MP4 Export** | Server-side: stitch frames into MP4 with smooth rotation (ffmpeg) |
| **Single Frame** | Download current frame as high-res JPEG |
| **Multi-Frame ZIP** | Download all 36 frames as ZIP |

#### 2C.2 — Social Sharing

- Open Graph meta tags on `/view/{vin}` page (og:image = frame 1, og:title = "2024 BMW M4 — LotVue360")
- Twitter Card meta tags
- "Share" button in viewer with: Copy Link, Facebook, Twitter/X, WhatsApp, Email
- UTM parameter support for tracking

---

### Phase 3: Dashboard & Dealer Management (PRIORITY 4)

#### 3.1 — Dealer Dashboard Pages

| Page | Contents |
|---|---|
| **Inventory** | Grid/list of all vehicles with thumbnail, VIN, status (active/draft/archived), view count |
| **Vehicle Detail** | Edit vehicle info, manage photos/video, preview viewer, copy embed code |
| **Leads** | Table of all inquiries from viewer forms, filter by vehicle/status/date, export CSV |
| **Analytics** | Views per vehicle, most viewed, lead conversion rate, embed views vs direct views |
| **Settings** | Dealer info, logo upload, notification email, background preferences, branding toggle |
| **Billing** | Current plan, usage, upgrade/downgrade, payment method (Stripe) |

#### 3.2 — Condition Report Builder

- In vehicle detail page, open a frame from the 360 spin
- Click to place a marker on the image
- Select condition type: Scratch, Dent, Chip, Tear, Stain, Crack, Other
- Add note text
- Markers appear as hotspots in the viewer with tooltip on hover
- Generates printable condition report PDF
- This is a HUGE trust builder for used car dealers

---

### Phase 4: Multi-Tenant SaaS (PRIORITY 5)

#### 4.1 — Authentication

- JWT-based auth with refresh tokens
- Roles: Admin (LotVue360 staff), Dealer Admin, Dealer User
- All data scoped by `dealer_id`
- Password reset flow
- Future: SSO for enterprise dealers

#### 4.2 — Stripe Billing

| Tier | Price | Includes |
|---|---|---|
| **Starter** | $49/mo | 25 vehicles, 360 spin only, LotVue watermark, basic backgrounds |
| **Pro** | $99/mo | 100 vehicles, full viewer (all tabs), no watermark, all backgrounds, lead capture, analytics |
| **Business** | $199/mo | Unlimited vehicles, custom branding, priority support, API access, condition reports |
| **Enterprise** | Custom | White-label, DMS integration, dedicated support, custom features |

#### 4.3 — Embed Customization per Dealer

- Color theme (primary color picker)
- Tab visibility toggles (show/hide any tab)
- Default view (which tab loads first)
- Language (EN/ES/FR for Phase 5)
- Custom CSS injection for enterprise tier

---

## PART 4: DATABASE SCHEMA (Build This From Day 1)

```sql
-- Dealers
CREATE TABLE dealers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    phone VARCHAR(20),
    website VARCHAR(255),
    logo_url VARCHAR(500),
    primary_color VARCHAR(7) DEFAULT '#0066FF',
    stripe_customer_id VARCHAR(255),
    plan VARCHAR(50) DEFAULT 'starter',
    notification_email VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Users (dealer staff)
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dealer_id UUID REFERENCES dealers(id),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    name VARCHAR(255),
    role VARCHAR(50) DEFAULT 'user', -- admin, dealer_admin, user
    created_at TIMESTAMP DEFAULT NOW()
);

-- Vehicles
CREATE TABLE vehicles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dealer_id UUID REFERENCES dealers(id),
    vin VARCHAR(17) UNIQUE NOT NULL,
    year INTEGER,
    make VARCHAR(100),
    model VARCHAR(100),
    trim VARCHAR(100),
    engine VARCHAR(100),
    transmission VARCHAR(50),
    drivetrain VARCHAR(50),
    fuel_type VARCHAR(50),
    body_style VARCHAR(50),
    exterior_color VARCHAR(50),
    interior_color VARCHAR(50),
    mileage INTEGER,
    price DECIMAL(10,2),
    description TEXT,
    status VARCHAR(20) DEFAULT 'draft', -- draft, active, archived, sold
    background_type VARCHAR(50) DEFAULT 'studio_shadow',
    custom_background_url VARCHAR(500),
    turntable_style VARCHAR(50) DEFAULT 'studio_shadow',
    show_interior_tab BOOLEAN DEFAULT FALSE,
    show_gallery_tab BOOLEAN DEFAULT TRUE,
    show_video_tab BOOLEAN DEFAULT FALSE,
    show_inquiry_tab BOOLEAN DEFAULT TRUE,
    video_url VARCHAR(500),
    video_type VARCHAR(20), -- upload, youtube, vimeo
    view_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Vehicle Frames (exterior 360 spin)
CREATE TABLE vehicle_frames (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id UUID REFERENCES vehicles(id) ON DELETE CASCADE,
    frame_number INTEGER NOT NULL,
    original_url VARCHAR(500) NOT NULL,
    processed_url VARCHAR(500), -- after bg removal + turntable
    aligned_url VARCHAR(500), -- after auto-alignment
    frame_type VARCHAR(20) DEFAULT 'exterior', -- exterior, interior_pano
    created_at TIMESTAMP DEFAULT NOW()
);

-- Vehicle Gallery Photos
CREATE TABLE vehicle_photos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id UUID REFERENCES vehicles(id) ON DELETE CASCADE,
    url VARCHAR(500) NOT NULL,
    thumbnail_url VARCHAR(500),
    category VARCHAR(50) DEFAULT 'exterior', -- exterior, interior, engine, trunk, damage, other
    sort_order INTEGER DEFAULT 0,
    caption VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW()
);

-- Interior Panoramas
CREATE TABLE vehicle_panoramas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id UUID REFERENCES vehicles(id) ON DELETE CASCADE,
    url VARCHAR(500) NOT NULL,
    scene_name VARCHAR(100) DEFAULT 'Front Seat',
    default_yaw FLOAT DEFAULT 0,
    default_pitch FLOAT DEFAULT 0,
    default_hfov FLOAT DEFAULT 120,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Hotspots (for both exterior and interior)
CREATE TABLE hotspots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id UUID REFERENCES vehicles(id) ON DELETE CASCADE,
    frame_number INTEGER, -- NULL for panorama hotspots
    panorama_id UUID REFERENCES vehicle_panoramas(id),
    x_position FLOAT NOT NULL, -- percentage 0-100 for frames, yaw for panoramas
    y_position FLOAT NOT NULL, -- percentage 0-100 for frames, pitch for panoramas
    hotspot_type VARCHAR(50) DEFAULT 'info', -- info, damage, feature, link
    title VARCHAR(255),
    description TEXT,
    image_url VARCHAR(500),
    severity VARCHAR(20), -- for damage: minor, moderate, severe
    created_at TIMESTAMP DEFAULT NOW()
);

-- Leads
CREATE TABLE leads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id UUID REFERENCES vehicles(id),
    dealer_id UUID REFERENCES dealers(id),
    vin VARCHAR(17),
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    phone VARCHAR(20),
    message TEXT,
    wants_video BOOLEAN DEFAULT FALSE,
    wants_test_drive BOOLEAN DEFAULT FALSE,
    source VARCHAR(50) DEFAULT 'viewer', -- viewer, embed, direct
    referrer VARCHAR(500),
    status VARCHAR(20) DEFAULT 'new', -- new, contacted, converted, lost
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Analytics Events
CREATE TABLE analytics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id UUID REFERENCES vehicles(id),
    dealer_id UUID REFERENCES dealers(id),
    event_type VARCHAR(50) NOT NULL, -- view, spin, interior_view, gallery_view, video_play, lead_submit, share, export
    tab_name VARCHAR(50),
    referrer VARCHAR(500),
    user_agent VARCHAR(500),
    ip_address VARCHAR(45),
    session_id VARCHAR(100),
    created_at TIMESTAMP DEFAULT NOW()
);

-- Backgrounds Library
CREATE TABLE backgrounds (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dealer_id UUID, -- NULL = system default, UUID = dealer custom
    name VARCHAR(100) NOT NULL,
    url VARCHAR(500) NOT NULL,
    thumbnail_url VARCHAR(500),
    category VARCHAR(50) DEFAULT 'studio', -- studio, outdoor, showroom, seasonal, custom
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW()
);
```

---

## PART 5: TECH STACK (Locked Decisions)

| Layer | Technology | Reason |
|---|---|---|
| Backend | **Python / FastAPI** | Already in use, keep it |
| Database | **PostgreSQL** | Upgrade from SQLite for multi-tenant |
| File Storage | **S3-compatible** (MinIO local or AWS S3) | Scalable media storage |
| Background Removal | **rembg** | Already working |
| Interior Panorama | **Pannellum 2.5.6** (CDN) | Free, open-source, lightweight |
| Auto-Alignment | **OpenCV** (cv2.findContours + bounding box) | Free, local processing |
| VIN Decoder | **NHTSA vPIC API** | Free government API |
| QR Generation | **qrcode** Python lib | Lightweight |
| GIF Export | **Pillow** | Already available |
| MP4 Export | **ffmpeg** | Already on system |
| Auth | **JWT** (python-jose) | Standard |
| Billing | **Stripe** | Industry standard |
| Email Notifications | **SendGrid** or **SMTP** | Lead notifications |
| Viewer Widget | **Vanilla JS + CSS** | Must be lightweight for iframe embed (<50KB) |
| Dashboard UI | **React** or **vanilla + HTMX** | Dealer-facing admin |

---

## PART 6: FILE STRUCTURE

```
lotvue360/
├── app/
│   ├── main.py                 # FastAPI app
│   ├── config.py               # Environment config
│   ├── database.py             # PostgreSQL connection
│   ├── models/                 # SQLAlchemy models
│   │   ├── dealer.py
│   │   ├── vehicle.py
│   │   ├── lead.py
│   │   ├── analytics.py
│   │   └── user.py
│   ├── routers/
│   │   ├── vehicles.py         # CRUD + upload
│   │   ├── viewer.py           # Public viewer endpoint
│   │   ├── leads.py            # Lead capture API
│   │   ├── analytics.py        # Event tracking
│   │   ├── auth.py             # Login/register/JWT
│   │   ├── dashboard.py        # Dealer dashboard API
│   │   ├── export.py           # GIF/MP4/QR generation
│   │   └── billing.py          # Stripe webhooks
│   ├── services/
│   │   ├── background_removal.py
│   │   ├── auto_alignment.py
│   │   ├── turntable.py
│   │   ├── vin_decoder.py
│   │   ├── email_service.py
│   │   └── export_service.py
│   ├── templates/
│   │   ├── viewer.html         # The embeddable viewer widget
│   │   └── dashboard/          # Dashboard HTML (if server-rendered)
│   └── static/
│       ├── viewer.js           # Viewer widget JS (bundled)
│       ├── viewer.css          # Viewer widget CSS
│       ├── turntables/         # Turntable graphic assets
│       └── backgrounds/        # Default background images
├── migrations/                 # Alembic DB migrations
├── tests/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## PART 7: WHAT MANUS SHOULD BUILD NEXT (Execution Order)

**DO NOT jump to Phase 4 (SaaS/billing). The viewer IS the product. If the viewer doesn't look as good as GLO3D, nobody will pay for anything.**

### Immediate Next Steps (in order):

1. **Migrate database to PostgreSQL** with the schema above
2. **Build the tabbed viewer UI** (Phase 2A.1) — HTML/CSS/JS only
3. **Wire exterior 360 tab** to existing spin code
4. **Add Pannellum interior tab** (Phase 2A.3)
5. **Add Gallery tab** (Phase 2A.4)
6. **Add Video tab** (Phase 2A.5)
7. **Add Lead Capture tab** (Phase 2A.6) + leads table + email notification
8. **Add virtual turntable** compositing (Phase 2B.1)
9. **Add VIN decoder** (Phase 2B.4)
10. **Add export formats** (Phase 2C.1)
11. **Polish dealer dashboard** (Phase 3.1)
12. **THEN and only then** — add auth + billing (Phase 4)

---

## PART 8: VIEWER EMBED CODE (Target Output)

When a dealer copies the embed code, it should look like this:

```html
<iframe 
  src="https://lotvue360.com/view/1HGCM82633A004352?dealer=drivesmart&theme=dark" 
  width="100%" 
  height="500" 
  frameborder="0" 
  allowfullscreen
  style="border-radius: 8px; max-width: 900px;">
</iframe>
```

The viewer URL should support query parameters:
- `dealer` — dealer slug for branding
- `theme` — `light` or `dark`
- `tab` — default tab (`exterior`, `interior`, `gallery`, `video`, `inquiry`)
- `autoplay` — `true` to auto-rotate exterior on load
- `zoom` — `true` to enable zoom
- `watermark` — `true`/`false` (overridden by plan tier)

---

## SUMMARY: Manus's Analysis vs Reality

| Manus Said | Reality | Action |
|---|---|---|
| 5-Tab Bottom Bar | ✅ Correct | Build it |
| Pannellum for interior | ✅ Correct | Build it |
| Gallery tab needed | ✅ Correct | Build it |
| Video tab needed | ✅ Correct | Build it |
| Lead capture needed | ✅ Correct but underspecified | Build with full form + email + dashboard |
| Virtual turntable | ✅ Correct but undervalued | Build it — it's the visual differentiator |
| "Mobile app later" | ✅ Correct for MVP | Web-first |
| Phase 4 first | ❌ WRONG | Viewer quality first, billing last |
| Pricing "$149-$899" | ❌ Outdated | DM alone is $395/mo, undercut at $99-199 |
| Missing: Guided Photography | ❌ Not mentioned | Add to upload wizard |
| Missing: Condition Reports | ❌ Not mentioned | Build hotspot-based damage markers |
| Missing: Auto-Alignment | ❌ Not mentioned | Critical for quality output |
| Missing: VIN Decoder | ❌ Not mentioned | Free API, high value |
| Missing: Export formats | ❌ Not mentioned | GIF/MP4/QR — easy wins |
| Missing: Analytics | ❌ Not mentioned | Track everything from Day 1 |

**Bottom line: Manus identified the viewer tabs correctly, but missed half the features that make dealers actually PAY. This spec fills every gap.**
