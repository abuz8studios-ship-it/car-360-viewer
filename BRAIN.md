# LotVue360 — CLAUDE.md
# REWRITTEN 2026-08-12 (never appended). Project scope only; identity = ~/.claude/CLAUDE.md.
# WARNING: two surfaces edit this repo (chat via MCP + Claude Code). READ DISK BEFORE EDITING.

## WHAT THIS IS
360/3D vehicle merchandising for independent car dealers — photo-spin + studio compositing,
with a premium Gaussian-splat tier from real walkaround video. Target: $99/mo/dealer.
Cloning the working parts of Spyne / CarCutter / GLO3D. NOT generative mesh — that tier failed QA.

## SOURCE OF TRUTH (run these; never quote a stored number)
- Git:     git -C E:\ABU\01_PRODUCTS\360Car-LotVue log -1 --oneline
- Routes:  python -c "from app.main import app; print(len(app.routes))"
- Public:  curl -s -o NUL -w "%{http_code}" https://lotvue.abuz8ai.com/
- Studio:  python app/studio.py --vin <VIN> --probe   (renders ONE frame, then STOP and LOOK)

## DELIVERY CONTRACT — "done" means all of these measured
- [x] validate_video.py gate rejects bad footage with a reason (2026-08-12: ORB
      continuity + car-present/crop/coverage/exposure; live 422 on DS8097, 200 on
      real walkaround; wired into /api/upload both branches, force=1 override)
- [ ] One VIN with a full studio-grade spin set, visually inspected at FULL RES
      (08-12: ONE frame vs all 40 bgs full-res, zero halos — full spin set still open)
- [x] Studio tier served by viewer_api manifest and live 200 on /vdp/<VIN>
- [~] Plate blur + hotspots (hotspot/damage layer DONE 08-12 incl. printable
      /vdp/{vin}/damage-report; plate blur still open)
- [ ] Pricing page live + Stripe checkout works end to end (OWNER GATE: key expired)
- [ ] LEDGER.md entry with sha256 + commit for the shipped build

## GATE THRESHOLDS (measured 2026-08-12 — see validate_video.py header)
- Continuity is THE walkaround-vs-synthetic discriminator: ORB RANSAC inliers on
  0.4s-apart pairs @640x360; weak pair < 200 inliers; FAIL when > 30% weak.
- Aspect-spread and bg-motion do NOT discriminate (DS8097: 0.822 / 0.154) —
  keep them only for static-camera and coverage failure modes.
