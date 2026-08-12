# LOTVUE PRO — GOAL ARTIFACT
> Rewritten 2026-08-12. One page. Definition of done — not a task list.

## THE GOAL
A real, sellable Spyne/Glo3D-parity product a dealer signs up for TODAY:
1. Capture a car on their phone (guided photo-spin walkaround)
2. Get back a studio-quality 360 spin with clean AI bg replacement
3. Optionally tag hotspots / damage points
4. It lands on their own VDP page AND pushes to Google Vehicle Listings
5. They pay $99/mo — fraction of Spyne's $350+ entry

Not a demo. Not localhost. Dealer types a URL, logs in, uploads, gets a
sellable asset back — end to end, zero humans in the loop on our side.

## DEFINITION OF DONE — the 60-second test, expanded
A dealer who has NEVER seen the product can, unaided:
1. Go to lotvue.abuz8ai.com, create account, land on working dashboard
   (not 401, not blank)
2. Upload a walkaround video of a real car
3. Bad capture → rejected with clear reason (validate_video.py working).
   Good capture → proceeds.
4. Get rendered 360 spin with bg replaced, in minutes not hours
5. See vehicle live on a public VDP URL they can share
6. See feed file ready for Google Vehicle Listings

IF ANY STEP NEEDS HAMADA / COWORK / CLAUDE TO INTERVENE, IT IS NOT DONE.

## PROOF STANDARD (three verifiable tool calls per claim, minimum)
Every "done" claim in the closing report carries:
1. LIVE PROBE — actual HTTP call to the actual running route (real status code)
2. DISK CHECK — grep/file-existence at the claimed path, live line-count,
   never a remembered number
3. OUTPUT CHECK — rendered artifact inspected at FULL resolution, never a
   thumbnail, never "the code should work"
Claims resting on fewer chains get GRADED and stated as such (isnad), not asserted.

## SUCCESS METRIC
Not "code complete." Not "builds clean." The metric:
Hamada can demo this live to his brother (Mint Comedy — standing first-customer
candidate) or a Dayton-area dealer, on a real car, zero prep, first take works.

## OUT OF SCOPE THIS PASS
- iOS build (owner-gated: Mac/cloud build)
- Ricoh Theta interior (hardware-gated)
- Matching Spyne's 100+ bg library (15-20 is v1 target)
- Generative mesh as standard tier (splat stays premium, real-footage-only, shipped)
