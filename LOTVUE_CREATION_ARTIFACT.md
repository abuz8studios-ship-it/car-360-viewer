# LOTVUE PRO — CREATION ARTIFACT
> Target: Spyne.ai / Glo3D feature parity at $99/mo vs their $350+/mo.
> Canonical build: E:\ABU\01_PRODUCTS\360Car-LotVue (junction E:\ABU\360Car → here). Dead: 01_PRODUCTS\PROJECTS\LOTVUE360.
> Rewritten 2026-08-12 (chat pass: competitor reverse-engineering + gap map). NEVER quote numbers from this file — re-measure live.

## 1. REVERSE-ENGINEERED COMPETITOR ARCHITECTURE (one search chain each, Aug 2026)

### Spyne.ai (chain: docs.spyne.ai + G2 + CarPixAI comparison)
- Capture: smartphone walkaround video OR guided photos. No pro camera.
- QUALITY GATE BEFORE PROCESSING: validate-video API rejects bad captures
  (cropping / car-present / angle coverage) at the front door. This is the step
  that would have auto-caught our 68 blob-GLB failure.
- Processing: async job — POST media → webhook on completion → GET asset.
- Outputs: AI studio-bg photos (100+ bgs), 360 spin, AI hotspots, Ricoh Theta interior.
- Pricing: Lite (photo+bg) / Pro (+tours, walkaround video, voice narration,
  AI descriptions), from ~$350/mo, sales-call gated above that.
- Customers: Cars24 IN/AU, Karvi, Sweep — multi-lot volume groups.

### Glo3D (chain: Apple App Store + G2 + glo3d.com feature pages)
- Capture: pure photo-spin, NO 3D reconstruction. Walk around car, app auto-fires
  24 FRAMES, AI auto-aligns/scales each frame live. Handheld OK, no gimbal.
- "Time Lapse" capture aid: each frame overlays a transparent ghost of the
  previous frame so operator matches spacing by eye. Pure UX, not processing.
- Output: stitched 360 spin up to 16K, multi-level zoom, GIF/MP4/embed export.
- Layers on top of spin: Interior Panorama, bg swap, Advanced Hotspot tech,
  Damage Report overlays, API/FTP push to DMS (WordPress/HomeNet/vAuto plugins).
- Distribution: auto-post to Facebook Marketplace + Google Vehicle Listings.
- Will deploy on dealer's own infra/CDN for compliance-bound dealers.

### THE ONE FACT THAT MATTERS MOST
NEITHER competitor generates a 3D mesh. Both = photo-spin (24-72 flat frames as
interactive turntable) + AI bg replacement + metadata/hotspot layer. This
independently confirms our 2026-08-11 pivot after the Hunyuan3D-2mv blob-GLB
failure. DO NOT RESUME GENERATIVE MESH for standard tier. Splat/GLB stays as
premium real-walkaround-only tier (proven on DSREAL01) — our edge OVER both.

## 2. GAP MAP — LotVue vs parity (disk-verified 2026-08-12)

| Capability | Spyne | Glo3D | LotVue | Gap |
|---|---|---|---|---|
| Photo-spin capture/stitch | Y | Y (24f) | SHIPPED (lotvue_viewer.js 3-tier) | none |
| AI bg replacement | Y 100+ | Y | SHIPPED (app/studio.py, pixel-verified shadow) | needs bg LIBRARY (have 1) |
| Quality gate pre-processing | Y | - | NOT BUILT (app/validate_video.py) | P0 — highest leverage |
| Hotspots / damage report | - | Y | not started | P1 |
| FB Marketplace auto-post | - | Y | not started | P1 (verify API access first) |
| Google Vehicle Listings feed | - | Y | not started | P1 (open spec, buildable now) |
| API/FTP push to DMS | Y | Y | partial (/crm exists, no outbound push) | P1 |
| Ricoh Theta interior | Y | - | not started | P2 (hardware-gated) |
| Interior panorama | - | Y | not started | P2 |
| Real-3D splat walkaround | - | - | SHIPPED (DSREAL01) | OUR EDGE — market it |
| iOS app | Y | Y | BLOCKED (needs Mac/cloud build) | owner gate |

## 3. BUILD ORDER FOR COWORK (each with verification bar)

### P0 — app/validate_video.py (quality gate)
- Input: uploaded walkaround video or photo batch.
- Checks: car-present (basic object detection), framing/crop adequacy, angle
  coverage (front/rear/both-sides/both-3-quarters minimum = ~8 angles).
- PASS → studio pipeline. FAIL → structured reason to capture UI
  ("car not centered", "missing rear angle").
- Verify (3 calls min): (1) known-good real walkaround set → PASS,
  (2) the synthetic-turntable set already on disk (caused COLMAP 2/N failure) → FAIL
  with reason, (3) grep live route registration proving it's wired into upload
  flow, not an orphan script.

### P1a — Background asset pack (15-20 bgs, not 100+)
- Categories: showroom floor, outdoor lot, urban, premium dark studio.
- Verify: render one vehicle against EVERY bg at FULL resolution, no halos.
  (Per 2026-08-12 lesson: never QA from a thumbnail.)

### P1b — Hotspot + damage report layer
- Model: hotspot = {x, y, frame_index, label, note, optional_photo}.
- Click-to-place on any spin frame, persists across frameset. Damage report =
  hotspots tagged damage + severity → printable summary page.
- Verify: create on live VDP, confirm DB persistence, confirm render at /vdp/<vin>.

### P1c — Distribution automation
- Google Vehicle Listings: structured feed spec, buildable now.
  Verify: generate one feed file, validate against Google's schema.
- FB Marketplace: RESEARCH ACCESS FIRST (partner-gated). If gated, MVP =
  manual export + scheduled reminder, and report the actual requirement found.

### P2 — Ricoh Theta / interior panorama (hardware owner-gated; scope software only)

## 4. DO-NOT LIST
- No generative mesh for standard tier (dead end, competitors don't do it either).
- No quoting baseline numbers without same-turn live measurement (NUMBERS RULE).
- No batching a new AI step across 200 vehicles before inspecting ONE full-res output.
- No building FB auto-post against assumed API access.
