# LotVue360 Customer Onboarding Runbook

## Beginning artifact

Starting state: A dealer has expressed interest, but inventory access, branding, billing, deployment, and acceptance are not yet verified.

Required intake:
- Dealership legal name and public website
- Primary owner, billing contact, and implementation contact
- Inventory source and authorized access method
- Store locations and approved domains
- Logo, brand colors, contact details, and legal disclosures
- Selected offer and explicitly approved price
- Payment status and service start date

## Deployment sequence

1. Confirm the dealer authorized inventory access and public display.
2. Record the selected offer, approved price, billing status, and cancellation terms.
3. Import a small inventory sample and validate VIN, price, mileage, photos, status, and vehicle URL.
4. Configure dealer branding and contact routing.
5. Generate a test vehicle page and inspect it on desktop and mobile.
6. Verify escaping of dealer-supplied text and safe rendering of vehicle data.
7. Confirm sold or removed inventory no longer appears active.
8. Obtain written dealer acceptance of the test page.
9. Activate the approved payment flow.
10. Publish only to the approved domain or embed location.
11. Save the live URL, timestamp, acceptance record, and billing receipt.
12. Schedule a 48-hour onboarding review.

## Customer welcome message

Subject: Your LotVue360 launch checklist

Your LotVue360 setup is ready to begin. Please send your dealership name, website, implementation contact, approved inventory access method, logo, brand colors, and preferred customer contact route. We will validate a small inventory sample first, send you a test vehicle page for acceptance, and publish only after the page and billing setup are confirmed.

## Goal-post artifact

Onboarding is finished only when:
- Authorized inventory data is loading correctly.
- A representative vehicle page passes desktop and mobile checks.
- Dealer branding and customer contact routing are correct.
- Sold and removed inventory behavior is verified.
- The dealer has accepted the test page in writing.
- The approved payment flow is active and receipted.
- The production URL is live and its body has been verified.
- Credentials are stored securely and are absent from this document.
- The 48-hour review is scheduled.

Evidence record:
- Dealer:
- Offer and approved price:
- Payment receipt:
- Test vehicle/VIN:
- Acceptance timestamp:
- Production URL:
- Live verification timestamp:
- 48-hour review date:
- Blockers:
