# PROMPT FOR COWORK — paste verbatim
> Written 2026-08-12 by chat. Companion to the three artifacts in this folder.

---

You are finishing LotVue Pro to sellable Spyne/Glo3D parity. Work from
E:\ABU\01_PRODUCTS\360Car-LotVue (canonical build — junction E:\ABU\360Car
points here; ignore any LOTVUE360 folder under PROJECTS, it is dead).

READ FIRST, IN ORDER, BEFORE ANY CODE:
1. LOTVUE_CANONICAL_LEDGER.md — current state, 19 rows, owner gates
2. LOTVUE_CREATION_ARTIFACT.md — competitor architecture + gap map + build order
3. LOTVUE_GOAL_ARTIFACT.md — definition of done + proof standard
4. CLAUDE.md in this folder — project conventions

RULES (non-negotiable):
- Disk outranks every document. Before building anything, verify the ledger
  row you depend on with a live probe or grep. If disk disagrees with the
  ledger, disk wins — correct the ledger row same turn.
- NEVER quote a number (route count, version, line count) from a file or
  memory. Measure it live in the same turn or label it "stale snapshot".
- DO NOT resume generative 3D mesh work for the standard tier. Row 10 is
  KILLED. Neither Spyne nor Glo3D generates meshes — parity is photo-spin.
- Every "done" claim needs THREE independent verifications: live HTTP probe
  + disk grep + full-resolution output inspection. Fewer than three = state
  the grade, don't assert.
- Never QA an image from a thumbnail. Full resolution only.
- Do not batch any new AI step across the vehicle set before rendering and
  inspecting ONE output.

BUILD ORDER (from LOTVUE_CREATION_ARTIFACT.md section 3 — do not reorder):
P0  app/validate_video.py — quality gate. Car-present, framing, angle-coverage
    checks on uploaded capture. PASS → pipeline, FAIL → structured reason.
    Test: known-good real walkaround → PASS; the synthetic-turntable set
    already on disk (the one that caused COLMAP 2/N) → FAIL with reason.
    Wire into the upload route. Prove wiring with a grep + live probe.
P1a Background asset pack — 15-20 backgrounds (showroom / outdoor lot /
    urban / dark studio) for app/studio.py. Render one vehicle against every
    bg at full res, zero halos, before calling it done.
P1b Hotspot + damage report layer — hotspot {x, y, frame_index, label, note,
    optional_photo}, click-to-place on spin frames, persists in DB, renders
    at /vdp/<vin>. Damage report = damage-tagged hotspots + severity as a
    printable summary page.
P1c Google Vehicle Listings feed — generate a valid feed file from live
    inventory, validate against Google's published schema.
    FB Marketplace: research current API/partner access requirements FIRST.
    If partner-gated, deliver manual-export MVP + document the requirement.

STOP CONDITIONS — do not attempt, flag as OWNER GATE instead:
iOS build, Stripe key refresh, store fees, DriveSmart permission, Ricoh
Theta hardware, any FB partner application requiring identity verification.

END OF SESSION (per standing protocol):
1. Update CLAUDE.md in this folder
2. Rewrite LOTVUE_CANONICAL_LEDGER.md rows you changed (rewrite, never stack)
3. Update SHA256_MANIFEST.txt and commit to git with a descriptive message
4. One sentence: exact next task
5. List any owner gates blocking

The finish line is LOTVUE_GOAL_ARTIFACT.md's 60-second test: a stranger
dealer signs up at lotvue.abuz8ai.com, uploads a walkaround, and gets a
sellable 360 asset back with zero human intervention. Work until every
non-owner-gated step of that test passes, then stop and report.
