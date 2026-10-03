"""
LotVue Pro — Listing Syndication
Export vehicle listings to marketplace formats:
  - Facebook Marketplace (catalog CSV)
  - Craigslist (HTML post)
  - CarGurus / AutoTrader (XML feed)
  - Generic embed (iframe-ready HTML)
  - JSON API (for custom integrations)

Zero cloud cost — all local generation.
"""
import os
import csv
import json
import io
from datetime import datetime
from pathlib import Path
from typing import Optional

from app_paths import STORAGE_DIR


def _get_photo_urls(vin: str, base_url: str = "") -> list:
    """Get list of photo URLs for a vehicle."""
    urls = []
    # Prefer studio photos
    studio = STORAGE_DIR / vin / "studio"
    exterior = STORAGE_DIR / vin / "exterior"

    source = studio if studio.exists() and list(studio.glob("*.jpg")) else exterior
    if source.exists():
        for f in sorted(source.glob("*.jpg"))[:20]:  # max 20 photos
            urls.append(f"{base_url}/storage/{vin}/{source.name}/{f.name}")
    return urls


def _build_description(vehicle: dict) -> str:
    """Build a clean listing description from vehicle data."""
    year = vehicle.get("year", "")
    make = vehicle.get("make", "")
    model = vehicle.get("model", "")
    trim = vehicle.get("trim_level", "")
    mileage = vehicle.get("mileage", "")
    engine = vehicle.get("engine", "")
    transmission = vehicle.get("transmission", "")
    drivetrain = vehicle.get("drivetrain", "")
    fuel = vehicle.get("fuel_type", "")
    color = vehicle.get("color", "")
    desc = vehicle.get("description", "")

    lines = [f"{year} {make} {model}"]
    if trim:
        lines[0] += f" {trim}"

    specs = []
    if mileage:
        specs.append(f"{mileage:,} miles" if isinstance(mileage, int) else f"{mileage} miles")
    if engine:
        specs.append(engine)
    if transmission:
        specs.append(transmission)
    if drivetrain:
        specs.append(drivetrain)
    if fuel:
        specs.append(fuel)
    if color:
        specs.append(f"Color: {color}")

    if specs:
        lines.append(" | ".join(specs))

    if desc:
        lines.append("")
        lines.append(desc)

    lines.append("")
    lines.append("Listed with LotVue Pro — AI-powered dealer platform")
    return "\n".join(lines)


def export_facebook_csv(vehicles: list, base_url: str = "") -> str:
    """
    Export vehicles as Facebook Marketplace catalog CSV.
    Facebook format: id, title, description, availability, condition, price, link, image_link, brand, ...
    """
    output = io.StringIO()
    writer = csv.writer(output)

    # Facebook catalog headers
    writer.writerow([
        "id", "title", "description", "availability", "condition",
        "price", "link", "image_link", "brand", "vehicle_id",
        "year", "make", "model", "mileage.value", "mileage.unit",
        "body_style", "drivetrain", "fuel_type", "transmission",
        "trim", "color", "state_of_vehicle"
    ])

    for v in vehicles:
        vin = v.get("vin", "")
        year = v.get("year", "")
        make = v.get("make", "")
        model = v.get("model", "")
        price = v.get("price", 0)
        photos = _get_photo_urls(vin, base_url)

        writer.writerow([
            vin,
            f"{year} {make} {model} {v.get('trim_level', '')}".strip(),
            _build_description(v),
            "in stock",
            "used",
            f"{price} USD" if price else "",
            f"{base_url}/view/{vin}",
            photos[0] if photos else "",
            make,
            vin,
            year,
            make,
            model,
            v.get("mileage", ""),
            "MI",
            v.get("body_style", ""),
            v.get("drivetrain", ""),
            v.get("fuel_type", ""),
            v.get("transmission", ""),
            v.get("trim_level", ""),
            v.get("color", ""),
            "Used"
        ])

    return output.getvalue()


def export_craigslist_html(vehicle: dict, base_url: str = "") -> str:
    """
    Generate Craigslist-ready HTML posting for a single vehicle.
    Craigslist accepts basic HTML in postings.
    """
    vin = vehicle.get("vin", "")
    year = vehicle.get("year", "")
    make = vehicle.get("make", "")
    model = vehicle.get("model", "")
    try:
        price = int(float(str(vehicle.get("price", 0) or 0).replace(",", "").replace("$", "").strip()))
    except (ValueError, TypeError):
        price = 0
    photos = _get_photo_urls(vin, base_url)

    title = f"{year} {make} {model}"
    if vehicle.get("trim_level"):
        title += f" {vehicle['trim_level']}"

    specs_html = ""
    specs = {
        "Year": year,
        "Make": make,
        "Model": model,
        "Trim": vehicle.get("trim_level", ""),
        "Mileage": f"{vehicle.get('mileage', '')} miles" if vehicle.get("mileage") else "",
        "Engine": vehicle.get("engine", ""),
        "Transmission": vehicle.get("transmission", ""),
        "Drivetrain": vehicle.get("drivetrain", ""),
        "Fuel Type": vehicle.get("fuel_type", ""),
        "Color": vehicle.get("color", ""),
        "VIN": vin,
    }
    for k, val in specs.items():
        if val:
            specs_html += f"<b>{k}:</b> {val}<br>\n"

    photos_html = ""
    for url in photos[:10]:
        photos_html += f'<img src="{url}" style="max-width:600px;margin:8px 0;">\n<br>\n'

    desc = vehicle.get("description", "")

    return f"""<h2>{title}</h2>
<p><b>Price: ${price:,}</b></p>

<p>{specs_html}</p>

{f'<p>{desc}</p>' if desc else ''}

{photos_html}

<p><i>Listed with LotVue Pro</i></p>
"""


def export_xml_feed(vehicles: list, dealer_info: dict = None, base_url: str = "") -> str:
    """
    Export vehicles as XML feed (compatible with CarGurus, AutoTrader, Cars.com).
    Uses ADF (Auto-lead Data Format) style structure.
    """
    dealer_name = (dealer_info or {}).get("name", "Dealer")
    dealer_id = (dealer_info or {}).get("id", "1")

    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append(f'<inventory dealer_id="{dealer_id}" dealer_name="{dealer_name}" '
                 f'generated="{datetime.now().isoformat()}">')

    for v in vehicles:
        vin = v.get("vin", "")
        photos = _get_photo_urls(vin, base_url)

        lines.append("  <vehicle>")
        lines.append(f"    <vin>{vin}</vin>")
        lines.append(f"    <stock_number>{v.get('stock_number', vin[-8:])}</stock_number>")
        lines.append(f"    <year>{v.get('year', '')}</year>")
        lines.append(f"    <make>{v.get('make', '')}</make>")
        lines.append(f"    <model>{v.get('model', '')}</model>")
        lines.append(f"    <trim>{v.get('trim_level', '')}</trim>")
        lines.append(f"    <body_style>{v.get('body_style', '')}</body_style>")
        lines.append(f"    <condition>Used</condition>")
        lines.append(f"    <price>{v.get('price', 0)}</price>")
        lines.append(f"    <mileage>{v.get('mileage', 0)}</mileage>")
        lines.append(f"    <engine>{v.get('engine', '')}</engine>")
        lines.append(f"    <transmission>{v.get('transmission', '')}</transmission>")
        lines.append(f"    <drivetrain>{v.get('drivetrain', '')}</drivetrain>")
        lines.append(f"    <fuel_type>{v.get('fuel_type', '')}</fuel_type>")
        lines.append(f"    <color>{v.get('color', '')}</color>")
        lines.append(f"    <description>{_build_description(v)}</description>")
        lines.append(f"    <url>{base_url}/view/{vin}</url>")

        if photos:
            lines.append("    <photos>")
            for url in photos:
                lines.append(f"      <photo>{url}</photo>")
            lines.append("    </photos>")

        lines.append("  </vehicle>")

    lines.append("</inventory>")
    return "\n".join(lines)


def export_embed_html(vehicle: dict, base_url: str = "") -> str:
    """
    Generate embeddable HTML widget for a vehicle listing.
    Dealers can paste this into their website.
    """
    vin = vehicle.get("vin", "")
    year = vehicle.get("year", "")
    make = vehicle.get("make", "")
    model = vehicle.get("model", "")
    try:
        price = int(float(str(vehicle.get("price", 0) or 0).replace(",", "").replace("$", "").strip()))
    except (ValueError, TypeError):
        price = 0
    photos = _get_photo_urls(vin, base_url)
    thumb = photos[0] if photos else ""

    return f"""<!-- LotVue Pro Embed — {year} {make} {model} -->
<div style="font-family:'DM Sans',sans-serif;max-width:400px;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;background:#F8FAFC;">
  <div style="position:relative;padding-top:66.67%;background:#e8ecf0;">
    <img src="{thumb}" style="position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;" alt="{year} {make} {model}">
  </div>
  <div style="padding:16px;">
    <h3 style="margin:0 0 4px;color:#2C3E50;font-size:18px;">{year} {make} {model}</h3>
    <p style="margin:0 0 8px;color:#6c757d;font-size:14px;">{vehicle.get('trim_level', '')} {'| ' + str(vehicle.get('mileage', '')) + ' mi' if vehicle.get('mileage') else ''}</p>
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <span style="font-size:22px;font-weight:700;color:#2E75B6;">${price:,}</span>
      <a href="{base_url}/view/{vin}" target="_blank" style="background:#2E75B6;color:#fff;padding:8px 16px;border-radius:8px;text-decoration:none;font-size:14px;">View Details</a>
    </div>
  </div>
</div>
"""


def export_json_api(vehicles: list, base_url: str = "") -> list:
    """
    Export vehicles as clean JSON for API integrations.
    """
    result = []
    for v in vehicles:
        vin = v.get("vin", "")
        photos = _get_photo_urls(vin, base_url)
        result.append({
            "vin": vin,
            "stock_number": v.get("stock_number", ""),
            "year": v.get("year", ""),
            "make": v.get("make", ""),
            "model": v.get("model", ""),
            "trim": v.get("trim_level", ""),
            "body_style": v.get("body_style", ""),
            "condition": "Used",
            "price": v.get("price", 0),
            "mileage": v.get("mileage", 0),
            "engine": v.get("engine", ""),
            "transmission": v.get("transmission", ""),
            "drivetrain": v.get("drivetrain", ""),
            "fuel_type": v.get("fuel_type", ""),
            "color": v.get("color", ""),
            "description": _build_description(v),
            "url": f"{base_url}/view/{vin}",
            "photos": photos,
            "listed_date": datetime.now().isoformat(),
        })
    return result
