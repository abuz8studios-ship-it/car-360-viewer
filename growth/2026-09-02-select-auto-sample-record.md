# First-dollar funnel — tracking record (per SALES_FUNNEL_FIRST_DOLLAR_2026-08-28.md "Tracking")

**Status: FREE SAMPLE PRODUCED. NOT SENT.** Sending is Ahmad's gate (outbound message on his behalf).

| Field | Value (verified 2026-09-02) |
|---|---|
| Dealer | Select Auto (independent used-car dealer) |
| Website | https://www.selectautoohio.com |
| Address / phone | 1713 Woodman Dr, Dayton, OH 45420 · 937-253-4300 (site) / (888) 656-3377 (cars.com) |
| Inventory count | 59 vehicles (their inventory page: "Page 1 of 5 (59 vehicles)"); cars.com shows 57 |
| Decision-maker | **NONE named anywhere** — not on their site, store page, cars.com, or search results. Yelp blocked (403). One review mentions an employee "Shawn". Channel = phone/text or the site's inquiry form. |
| Sample vehicle | 2021 Chevrolet Camaro 2dr Cpe 1LT · $24,995 · 64,940 mi · stock 24218105 |
| Sample VDP URL | https://www.selectautoohio.com/vdp/24218105/Used-2021-Chevrolet-Camaro-2dr-Cpe-1LT-for-sale-in-Dayton-OH-45420 |
| Source photos | 42 public gallery photos (imagescdn.dealercarsearch.com/Media/2182/24218105/*.jpg), 1280px |
| Pipeline | `app\studio_spin.py --dir <photos> --vin SELECTAUTO24218105 --bg showroom_floor` (CLIP select → whole-car pass → ORB order → studio composite), rembg on CPU |
| Output | `app\storage\SELECTAUTO24218105\studio_spin\{000-006.jpg, showroom_floor.mp4, meta.json}` |
| Quality verdict (eye-checked) | **4 of 7 angles clean** (000 front, 002 front-¾, 005 profile, 006 rear-¾). 001 = doors-open frame, 003 = light-pole remnant above roof, 004 = two other lot cars kept by the matte. The mp4 contains all 7 → do NOT send the mp4. Send the 4 stills. |
| Deliverable | `select_auto_camaro_4_studio_angles.jpg` + `before_after.jpg` (scratchpad, copy into this folder when sending) |
| Sample-delivery date | — (not sent) |
| Response | — |
| Paid-order status | — |
| Retainer status | — |

## Three merchandising findings — observable only, no invented facts
1. **No 360 / walkaround on the listing.** The VDP has no spin, 360, or panorama feature (the word "spin" appears 20× in the HTML — all are loading-spinner CSS classes, not a viewer). Buyers get flat photos only.
2. **Only 7 of 42 photos show the whole car.** CLIP classification of the gallery: 14 exterior / 19 interior / 9 detail; of the 14 exterior, 7 are close-ups or part shots, and of the 7 whole-car shots 3 are doors-open, far-away, or cluttered with neighboring cars. A shopper cannot walk around this car on the page.
3. **Conversion CTAs are thin.** Present: "Make An Offer", "Text Us", "Value Your Trade". Absent: "Schedule Test Drive", "Check Availability", "Get Pre-Approved". Price and mileage are shown (good).

## Draft outreach (Ahmad sends — do not send from any agent)
Channel: their site's vehicle-inquiry form on the Camaro VDP, or text to 937-253-4300. No email address is published.

> **Subject: I built a free vehicle-page upgrade for Select Auto**
>
> I reviewed your 2021 Camaro 1LT (stock 24218105) on selectautoohio.com and prepared studio-floor versions of your own listing photos — same car, same photos, cleaner presentation. Attached: four angles, before/after.
>
> Three things I noticed on the listing: there's no 360 view, only 7 of the 42 photos show the whole car, and there's no "schedule a test drive" button. Those are fixable this week.
>
> The sample is free, no strings. If you want it applied across your inventory, the fixed launch is $1,000 (dealer branding, inventory presentation setup, deployment, one verified live example on your site). Ongoing monitoring and updates are $500/month. No contract for the sample.
>
> Reply SAMPLE with the next vehicle you want done, or call/text me.
>
> Ahmad Odeh · ABUZ8 LLC · Dayton, Ohio · abuz8studios@gmail.com
> (The presentation work is AI-assisted; every image is derived from your own photos.)

## Pricing conflict to resolve before sending (three different numbers on disk)
- `SALES_FUNNEL_FIRST_DOLLAR_2026-08-28.md`: $1,000 launch + **$500/mo** retainer  ← used above
- `SALES_FUNNEL_LOTVUE360_2026-08-28.md`: $1,000 sprint + **$1,000/mo**
- `BRAIN.md` / runbook item #5: **$99/mo/dealer** SaaS
Ahmad picks one. Do not send with a number he hasn't confirmed.

## Warm lead the funnel docs never named (found in `app\BUILD_SESSION_LOG.md:253`)
**Mohammed — Drive Smart Auto Sales — wsuodeh@gmail.com** — already onboarded as a dealership user in `vehicles.db` (999,999 credits), 200 of his vehicles were imported by the DriveSmart scraper (ledger row 4). Shortest path to a first dollar on disk. Ahmad's call whether the first $1,000 ask goes to family/warm (Mohammed) or stranger/cold (Select Auto). Note the ledger's owner gate: "DriveSmart written permission for scraped inventory as GTM dataset."

## Blockers that stop a paid order even if a dealer says yes
- Stripe secret key in `app\.env` → live-tested `401 Expired API Key` (Ahmad: roll key in Stripe dashboard, paste as `STRIPE_SECRET_KEY`).
- Gmail app password in `app\.env` → live-tested `535 BadCredentials` (Ahmad: new app password → `LOTVUE_SMTP_PASS`). Until fixed, no lead alert email fires.
- `LOTVUE\StartApp` scheduled task is Disabled → server dies on reboot (Ahmad: yes/no to re-enable; persistent config change).
