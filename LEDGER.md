
## 2026-08-12 — LotVue Studio layer (Spyne/CarCutter parity)
BUILT: app/studio.py (206 lines) — the AI polish layer.
  segment (rembg/u2net) -> alpha refine (erode 1px + feather 1.2px, kills white halo)
  -> synthesized elliptical contact shadow -> composite onto studio bg
  -> optical-flow (Farneback) interpolation to N frames = "hypersmooth"
  -> meta.json per vehicle
DOCTRINE ENFORCED IN CODE: no generative mesh. AI segments, it does not invent.
  --probe renders ONE frame and stops. Direct consequence of the 2026-08-11 GLB blob failure.
TRAP HANDLED: cudnn64_9.dll missing on Pegasus -> CUDA provider throws NOT_IMPLEMENTED on
  Conv_0. studio.py now tries CUDA, live-tests it with a 32x32 dummy, falls back to CPU.
PROBE RESULT (sahih, live): VIN 00000000000576449, 1280x854, 2.3s/frame CPU.
  Rendered and VISUALLY INSPECTED: cutout clean on a hard subject (BMW Isetta — chrome
  bumper, detached headlight pods, mirror stalks, negative space all preserved). No halo.
KNOWN DEFECT (found by looking, not by testing): contact shadow too faint, car reads as
  floating. Fix = raise strength 0.55->0.80 and lift ellipse into the tyre line. NOT yet done.
COMPETITOR CHAIN (one chain, live-fetched spyne.ai/360-car-photography 2026-08-12):
  Spyne pricing starts $350/mo, tiers Bronze/Silver/Gold/custom. Their pipeline = walkaround
  capture -> AI -> spin + background replacement (100+ backgrounds) + AI hotspots +
  "automatic catalog" (spin generated FROM video) + Ricoh Theta interior.
  => Confirms photo-spin + bg-replace is the industry-correct architecture, NOT generative mesh.
  => $99/mo LotVue target sits ~3.5x under Spyne's entry. Verify GLO3D/CarCutter live before quoting.
NEXT: (1) shadow fix + re-probe, (2) studio_bg asset pack, (3) full-set batch on one VIN,
  (4) wire studio tier into viewer_api manifest, (5) plate blur, (6) hotspots.

## 2026-08-12 (cont) — shadow fix + INPUT GATE
SHADOW FIXED: contact_shadow strength 0.55->0.82, squash 0.11->0.13, ellipse centre
  lifted (y1 - 0.82*sh to y1 + 0.35*sh) so the dark band sits at the tyre contact patch.
  Re-probed + VISUALLY INSPECTED: car now reads grounded, not floating. Grade sahih.
BUILT: app/validate.py (149 lines) — the INPUT GATE, modeled on Spyne's
  POST /360/v1/validate-video (live-fetched docs.spyne.ai 2026-08-12), which gates on
  Cropping / Angle / Car Present BEFORE processing. Ours measures:
    CAR_PRESENT  subject area within 6%-92% of frame
    CROPPING     subject touching left/right/top edge (bottom excluded — cars sit on ground)
    ANGLE        aspect-ratio spread across the sampled set >= 0.10. THIS is the
                 synthetic-turntable detector: static camera = silhouette barely changes
                 = geometrically unsolvable for COLMAP. Would have caught the Aug-10 failure.
    EXPOSURE     mean luma 28..232
  Emits splat_eligible flag so the splat lane can never be fed unsolvable input.
FIRST RUN (sahih, live): VIN 00000000000576449, 36 frames.
  aspect_spread=0.425 -> genuine angular spin, PASSES the turntable check.
  3/6 sampled frames FAIL "cropped: top" -> real defect found in real dealer data.
  Set verdict FAIL, splat_eligible=False. The gate is working as designed.
SPYNE API SURFACE (mapped from their docs, one chain): async job + webhook model.
  Studio AI: Merchandise Process (POST) / Retrieve Media (GET).
  v1: Catalog Generation (Classify Images, Transform Images, Webhook, GET Images);
  360-Spin (Validate Video, Webhook-360, 3D 360-Spin, Response 360-Spin);
  Feature Video (Video Generation, Response Videos); Verify Your Signature.
  => Our API should mirror: submit job -> job id -> webhook/poll -> retrieve media.
NEXT: (1) studio_bg asset pack, (2) full-set studio batch on this VIN,
  (3) wire studio tier + validate gate into viewer_api manifest, (4) async job+webhook API
  shape mirroring Spyne, (5) plate blur, (6) hotspots.

## 2026-08-12b — shadow tuning + a method lesson
FIXED: contact_shadow now strength 0.88 / squash 0.045 / blur sh*0.32, ellipse centred ON the
  tyre line (half hidden behind the car). Prior 0.11-0.13 squash made a 126px ellipse with a
  50px blur spanning y585-822 — a haze that dissolved into the bg gradient.
VERIFIED BY MEASUREMENT, not by eye: probe output pixels under the car =
  y735 x640 [70,73,77] vs background y780 [200,205,211]. Shadow is dark and present.
  Full-res crop (120,600,1140,854) confirms grounding shadow at bumper + both tyres.
METHOD LESSON (costly, worth keeping): I judged "still floating" TWICE from a 700px thumbnail
  of an 854px frame. A 13px contact band is invisible at that scale. The parameter was already
  fine after the first change. RULE: for any pixel-level QA, MEASURE the pixels or crop at full
  res — a downscaled thumbnail is not evidence. Same error class as quoting a baseline file
  instead of measuring the live source.
CONCURRENCY NOTE: between my write and my next edit, another surface (Claude Code on Pegasus)
  had already edited contact_shadow to strength 0.82. Disk won; I merged against actual state
  rather than overwriting. Two surfaces are editing this repo — check disk before assuming.
SPYNE API SURFACE MAPPED (docs.spyne.ai, live 2026-08-12): validate-video (checks Cropping,
  Angle, Car Present BEFORE processing) -> 3D 360-spin (POST) -> webhook push -> GET response.
  Async job pattern with a QUALITY GATE AT THE FRONT — reject bad footage before spending GPU.
  NOT YET BUILT on our side. This is the next module: app/validate_video.py.
NEXT: (1) validate_video.py gate, (2) studio_bg asset pack, (3) full-set batch one VIN,
  (4) wire studio tier into viewer_api manifest, (5) plate blur, (6) hotspots.

2026-09-02 | Server restarted (was 502 since ≥08-12; StartApp task Disabled) via start_server.bat; Stripe key live-tested 401 Expired; SMTP 535 BadCredentials; Select Auto 2021 Camaro free-sample spin rendered (7 angles, storage\SELECTAUTO24218105\studio_spin) | probe: local /api/health 200, public 200 | no commit (server state + storage output, not source)
