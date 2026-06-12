"""
Registers the production Stripe webhook endpoint for LotVue360 and stores the
signing secret in app/.env. Secrets stay disk -> disk; prints booleans only.
Run:  E:\\Miniconda3\\envs\\lotvue\\python.exe setup_stripe_webhook.py
"""
import re
from pathlib import Path

import stripe

APP_DIR = Path(__file__).resolve().parent
ENV = APP_DIR / ".env"
WEBHOOK_URL = "https://lotvue.abuz8ai.com/api/stripe/webhook"

env_text = ENV.read_text(encoding="utf-8")
sk = re.search(r"^STRIPE_SECRET_KEY=(.+)$", env_text, re.M)
if not sk:
    raise SystemExit("no STRIPE_SECRET_KEY in .env")
stripe.api_key = sk.group(1).strip()

have_secret_in_env = bool(re.search(r"^STRIPE_WEBHOOK_SECRET=whsec_", env_text, re.M))

existing = stripe.WebhookEndpoint.list(limit=50)
target = None
for ep in existing.auto_paging_iter():
    if ep["url"] == WEBHOOK_URL:
        target = ep
        break

created = False
if target is not None and not have_secret_in_env:
    # Only recycle the endpoint THIS tool created moments ago (crashed before
    # capturing the secret): must match our description AND be <30 min old.
    import time
    desc = ""
    try:
        desc = target["description"] or ""
    except KeyError:
        pass
    age_min = (time.time() - target["created"]) / 60
    if "auto-registered" in desc and age_min < 24 * 60:
        stripe.WebhookEndpoint.delete(target["id"])
        target = None
    else:
        print("endpoint pre-existed and is not ours — NOT deleting:", target["id"])

if target is None:
    target = stripe.WebhookEndpoint.create(
        url=WEBHOOK_URL,
        enabled_events=["checkout.session.completed"],
        description="LotVue360 production webhook (auto-registered)",
    )
    created = True

try:
    secret = target["secret"] or ""  # only present at creation
except KeyError:
    secret = ""
if secret:
    if "STRIPE_WEBHOOK_SECRET=" in env_text:
        env_text = re.sub(r"^STRIPE_WEBHOOK_SECRET=.*$", f"STRIPE_WEBHOOK_SECRET={secret}", env_text, flags=re.M)
    else:
        env_text += f"\nSTRIPE_WEBHOOK_SECRET={secret}\n"
    ENV.write_text(env_text, encoding="utf-8")

print("webhook endpoint id:", target["id"])
print("url:", target["url"])
print("created new:", created)
print("status:", target["status"])
print("signing secret captured to .env:", bool(secret))
if not secret and not created:
    print("NOTE: endpoint pre-existed; Stripe only reveals the secret at creation. "
          "If .env still lacks STRIPE_WEBHOOK_SECRET, roll the secret in the Stripe "
          "dashboard or delete+rerun this script to recreate the endpoint.")
