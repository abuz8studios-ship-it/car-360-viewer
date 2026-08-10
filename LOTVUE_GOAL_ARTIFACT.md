# LOTVUE PRO — GOAL ARTIFACT
> What "finished" means. Locked 2026-08-10. Changes to this file require Hamada's sign-off.

## THE ONE SENTENCE
A dealer signs in on desktop, phone, or web; walks a car with the phone; and minutes later that car is a real
interactive 3D vehicle on their website with an AI salesperson attached to it that books test drives while they sleep.

## THE THREE SURFACES — all one account, one backend

**DESKTOP (Windows .exe + installer)** — the dealer's control room. Full inventory, 3D studio, CRM,
batch processing on local GPU. Signs in to the same web tier. Works when the internet blips.

**WEB (lotvue.abuz8ai.com)** — the buyer-facing surface. The 3D VDP that embeds into the dealer's existing
WordPress/DealerCenter site by iframe or VIN-keyed embed. Zero install for the shopper.

**MOBILE (iOS + Android, in the stores)** — the capture tool. Guided 12-angle walkaround, VIN barcode scan,
live upload. This is the piece that makes the whole thing take minutes instead of a photo shoot.

## ACCEPTANCE TESTS — binary, no partial credit

1. Cold-boot the packaged `.exe` on a machine that has never run LotVue → dealer dashboard loads, sign-in works.
2. Install the mobile app from a store link (or signed TestFlight/internal track) → scan a VIN → shoot the
   guided sequence → the vehicle appears in the desktop dashboard without any manual step.
3. Open a real DriveSmart vehicle's VDP in a browser → orbit it with a mouse, pinch-zoom on a phone, change the
   paint, open a door hotspot, step inside the interior 360.
4. A stranger asks that VDP a question → the AI BDC answers from that vehicle's actual specs and offers a
   test-drive slot → the lead lands in the CRM inbox with a score attached.
5. Every row of the GLO3D parity matrix carries a live HTTP status code, not a checkmark someone typed.
6. `Stripe checkout → paid → account provisioned` completes end to end with a real card.

## WHAT WE BEAT AND HOW

GLO3D sells this as seven products ($395 + $944 + $247 + $198 + $497 + $148/mo, credit-metered, opaque).
LotVue is **one product, one flat price, self-serve.** Our three things they cannot answer:
AI-generated video commercials (WAN 2.2 + Kokoro TTS), a bundled desktop program that uses the dealer's own GPU,
and real GLB/PBR 3D rather than a photo carousel.

## PRICE
$99/mo entry (named target, unchanged). The bundle GLO3D charges ~$2,400/mo for.

## NOT IN SCOPE (say no on purpose)
In-platform ad buying · full DMS write-back · multi-tenant white-label reseller tier · Android tablet-specific UI.
These come after the first paying dealer, not before.

## OWNER GATES ON THE GOAL
Apple Developer ($99/yr) · Google Play Console ($25) · live Stripe key · Cloudflare tunnel ·
written DriveSmart permission to scrape and republish.
