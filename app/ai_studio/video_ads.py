"""
LotVue Pro — AI Video Ads + Personalized Lead Response (Module 10)
Cloning best of Impel AI + AIVO360:
  - Per-vehicle video ads with AI voiceover
  - Personalized video response per lead inquiry
  - Digital dossier page (payment calc + 360 link + specs)

Uses existing ComfyUI + Kokoro TTS + FFmpeg pipeline.
Zero cloud cost — all local generation.
"""
import json
import os
import subprocess
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from app_paths import STORAGE_DIR


def generate_video_ad_script(vehicle: dict, ad_type: str = "listing",
                              dealer_name: str = "LotVue Pro Dealer") -> dict:
    """
    Generate a video ad script for a vehicle.
    ad_type: 'listing' (marketplace ad), 'social' (short-form),
             'personalized' (for a specific lead)
    """
    year = vehicle.get("year", "")
    make = vehicle.get("make", "")
    model = vehicle.get("model", "")
    trim = vehicle.get("trim_level", "")
    title = f"{year} {make} {model}"
    if trim:
        title += f" {trim}"

    try:
        price = int(float(str(vehicle.get("price", 0) or 0).replace(",", "").replace("$", "").strip()))
    except (ValueError, TypeError):
        price = 0

    mileage = vehicle.get("mileage", "")
    engine = vehicle.get("engine", "")
    color = vehicle.get("color", "")

    scripts = {
        "listing": {
            "duration": 30,
            "scenes": [
                {
                    "shot": "exterior_hero",
                    "duration": 5,
                    "voiceover": f"Introducing the {title}.",
                    "text_overlay": title,
                },
                {
                    "shot": "exterior_360",
                    "duration": 8,
                    "voiceover": (
                        f"{'Finished in ' + color + ', this' if color else 'This'} "
                        f"{make} {model} features "
                        f"{'a ' + engine + ' engine' if engine else 'impressive performance'}"
                        f"{' with just ' + str(mileage) + ' miles' if mileage else ''}."
                    ),
                    "text_overlay": f"{mileage} miles" if mileage else "",
                },
                {
                    "shot": "interior",
                    "duration": 7,
                    "voiceover": (
                        f"Step inside and experience premium comfort "
                        f"{'with the ' + trim + ' package' if trim else 'and modern technology'}."
                    ),
                    "text_overlay": trim if trim else "",
                },
                {
                    "shot": "pricing_cta",
                    "duration": 5,
                    "voiceover": (
                        f"{'Priced at just ' + '${:,}'.format(price) if price else 'Call for pricing'}. "
                        f"Contact {dealer_name} today."
                    ),
                    "text_overlay": f"${price:,}" if price else "Call for pricing",
                },
                {
                    "shot": "dealer_logo",
                    "duration": 5,
                    "voiceover": f"Visit {dealer_name} or schedule your test drive online now.",
                    "text_overlay": dealer_name,
                },
            ],
        },
        "social": {
            "duration": 15,
            "scenes": [
                {
                    "shot": "exterior_hero",
                    "duration": 4,
                    "voiceover": f"Check out this {title}!",
                    "text_overlay": title,
                },
                {
                    "shot": "exterior_360",
                    "duration": 4,
                    "voiceover": (
                        f"{'Only ' + str(mileage) + ' miles.' if mileage else 'Low mileage.'} "
                        f"{'Just ' + '${:,}'.format(price) + '.' if price else ''}"
                    ),
                    "text_overlay": f"${price:,}" if price else "",
                },
                {
                    "shot": "pricing_cta",
                    "duration": 4,
                    "voiceover": "Link in bio to see the full walkaround!",
                    "text_overlay": "Link in Bio",
                },
                {
                    "shot": "dealer_logo",
                    "duration": 3,
                    "voiceover": dealer_name,
                    "text_overlay": dealer_name,
                },
            ],
        },
        "personalized": {
            "duration": 25,
            "scenes": [
                {
                    "shot": "greeting",
                    "duration": 5,
                    "voiceover": f"Hi! Thanks for your interest in the {title}.",
                    "text_overlay": f"Your {title}",
                },
                {
                    "shot": "exterior_360",
                    "duration": 8,
                    "voiceover": (
                        f"Here's a closer look at this beautiful "
                        f"{'gorgeous ' + color + ' ' if color else ''}{make} {model}. "
                        f"{'It has just ' + str(mileage) + ' miles' if mileage else 'Low mileage'} "
                        f"and is in excellent condition."
                    ),
                    "text_overlay": f"{mileage} mi | {engine}" if mileage else "",
                },
                {
                    "shot": "interior",
                    "duration": 5,
                    "voiceover": "The interior is clean and well-maintained, ready for you.",
                    "text_overlay": "",
                },
                {
                    "shot": "pricing_cta",
                    "duration": 7,
                    "voiceover": (
                        f"{'At ' + '${:,}'.format(price) + ', this is a great value.' if price else 'Contact us for special pricing.'} "
                        f"I'd love to get you behind the wheel for a test drive."
                    ),
                    "text_overlay": f"${price:,}" if price else "Special Pricing",
                },
            ],
        },
    }

    script = scripts.get(ad_type, scripts["listing"])

    # Full voiceover text for TTS
    full_voiceover = " ".join(s["voiceover"] for s in script["scenes"])

    return {
        "vehicle_title": title,
        "ad_type": ad_type,
        "total_duration": script["duration"],
        "scenes": script["scenes"],
        "full_voiceover": full_voiceover,
        "generated_at": datetime.now().isoformat(),
    }


def generate_personalized_response(lead: dict, vehicle: dict,
                                    dealer_name: str = "LotVue Pro Dealer",
                                    base_url: str = "") -> dict:
    """
    Generate a complete personalized lead response package:
    - Video ad script customized for the lead
    - Digital dossier link
    - SMS/email with video link
    """
    customer_name = lead.get("name", "").split()[0] if lead.get("name") else "there"
    vin = vehicle.get("vin", "")

    # Generate personalized video script
    script = generate_video_ad_script(vehicle, ad_type="personalized", dealer_name=dealer_name)

    # Customize greeting with customer name
    if script["scenes"] and customer_name != "there":
        script["scenes"][0]["voiceover"] = (
            f"Hi {customer_name}! Thanks for your interest in the "
            f"{script['vehicle_title']}."
        )

    # Build response package
    return {
        "lead_name": lead.get("name", ""),
        "vehicle_title": script["vehicle_title"],
        "video_script": script,
        "dossier_url": f"{base_url}/dossier/{vin}?lead={lead.get('id', '')}",
        "video_url": f"{base_url}/api/walkaround/{vin}/video",
        "viewer_360_url": f"{base_url}/360-viewer/{vin}",
        "sms_message": (
            f"Hi {customer_name}! Here's a personal video of the "
            f"{script['vehicle_title']} you asked about: "
            f"{base_url}/dossier/{vin} — {dealer_name}"
        ),
        "email_subject": f"{customer_name}, your {script['vehicle_title']} video is ready",
        "email_body": (
            f"Hi {customer_name},\n\n"
            f"I put together a personalized video walkaround of the "
            f"{script['vehicle_title']} just for you.\n\n"
            f"Watch your video: {base_url}/api/walkaround/{vin}/video\n"
            f"Interactive 360° view: {base_url}/360-viewer/{vin}\n"
            f"Full details & payment calculator: {base_url}/dossier/{vin}\n\n"
            f"Ready for a test drive? Just reply to this email!\n\n"
            f"Best,\n{dealer_name}"
        ),
        "generated_at": datetime.now().isoformat(),
    }


def get_video_ad_status(vin: str) -> dict:
    """Check status of video ad generation for a vehicle."""
    video_dir = STORAGE_DIR / vin / "video_ads"
    if not video_dir.exists():
        return {"status": "not_started", "videos": []}

    videos = []
    for f in sorted(video_dir.glob("*.mp4")):
        videos.append({
            "filename": f.name,
            "path": str(f),
            "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
            "url": f"/storage/{vin}/video_ads/{f.name}",
            "created": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        })

    return {
        "status": "complete" if videos else "not_started",
        "count": len(videos),
        "videos": videos,
    }
