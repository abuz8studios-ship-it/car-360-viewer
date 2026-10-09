"""
C-Phase Intelligence Extensions — LotVue360
C1: Market pricing via Marketcheck (licensed API; dark until MARKETCHECK_API_KEY set,
    with a local-comps fallback estimator so the feature works day one)
C2: Vehicle data aggregation — LEGAL SOURCES ONLY:
      - NHTSA vPIC (VIN decode, public US-gov API)
      - NHTSA recalls API (public)
      - fueleconomy.gov (public EPA API)
      - Marketcheck (licensed, when credentialed)
    No scraping of sites whose ToS prohibit it.
C3: Auction integration framework — pluggable adapters (Manheim / ADESA / Copart),
    every adapter DARK until its credentials are configured. No stub pretends to work.

Wired in from main.py via:  from c_intelligence import register_intelligence; register_intelligence(app)
"""

import os
import re
import json
import sqlite3
import statistics
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from app_paths import BASE_DIR, DB_PATH

router = APIRouter()


def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _vehicle(vin: str):
    vin = (vin or "").strip().upper()
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        raise HTTPException(404, "Vehicle not found")
    conn = _db()
    v = conn.execute("SELECT * FROM vehicles WHERE vin=? AND status='active'", (vin,)).fetchone()
    conn.close()
    if not v:
        raise HTTPException(404, "Vehicle not found")
    return {k: v[k] for k in v.keys()}


def _fetch_json(url: str, headers: dict = None, timeout: int = 20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=max(1, min(timeout, 30))) as r:
        raw = r.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("External data response is too large")
    return json.loads(raw)


def _epa_fuel_economy(year: str, make: str, model: str) -> dict | None:
    query = urllib.parse.urlencode({"year": year, "make": make, "model": model})
    options = _fetch_json(
        f"https://www.fueleconomy.gov/ws/rest/vehicle/menu/options?{query}",
        headers={"Accept": "application/json"},
    )
    items = options.get("menuItem") or []
    if isinstance(items, dict):
        items = [items]
    if not items:
        return None
    vehicle_id = str(items[0].get("value", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", vehicle_id):
        return None
    economy = _fetch_json(
        f"https://www.fueleconomy.gov/ws/rest/vehicle/{vehicle_id}",
        headers={"Accept": "application/json"},
    )
    return {"city": economy.get("city08"), "highway": economy.get("highway08"),
            "combined": economy.get("comb08"), "fuel": economy.get("fuelType")}


# =============================================================================
# C1 — MARKET PRICING (Marketcheck)
# =============================================================================
MARKETCHECK_KEY = os.getenv("MARKETCHECK_API_KEY", "")
MARKETCHECK_HOST = os.getenv("MARKETCHECK_HOST", "https://mc-api.marketcheck.com/v2")


@router.get("/api/pricing/status")
def pricing_status():
    return {
        "marketcheck_configured": bool(MARKETCHECK_KEY),
        "how_to": "Set MARKETCHECK_API_KEY env var. Plans at marketcheck.com/apis "
                  "(licensed listings data — the legal equivalent of scraping the majors).",
        "fallback": "Local-comps estimator (median of same make/model in your own inventory).",
    }


def _marketcheck_comps(v: dict, radius: int = 100, zip_code: str = ""):
    params = {
        "api_key": MARKETCHECK_KEY,
        "year": v.get("year") or "", "make": v.get("make") or "", "model": v.get("model") or "",
        "car_type": "used", "rows": 25, "stats": "price,miles",
    }
    if zip_code:
        params["zip"] = zip_code
        params["radius"] = radius
    url = f"{MARKETCHECK_HOST}/search/car/active?{urllib.parse.urlencode(params)}"
    return _fetch_json(url, timeout=25)


@router.get("/api/pricing/{vin}/market")
def market_pricing(vin: str, zip_code: str = "", radius: int = 100):
    """Market price intelligence for a vehicle. Marketcheck when credentialed,
    otherwise a clearly-labelled local-comps estimate."""
    vin = (vin or "").strip().upper()
    if zip_code and (len(zip_code) > 16 or not re.fullmatch(r"[A-Za-z0-9 -]+", zip_code)):
        raise HTTPException(400, "Invalid location code")
    try:
        radius = int(radius)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "Radius must be between 1 and 500 miles") from exc
    if not 1 <= radius <= 500:
        raise HTTPException(400, "Radius must be between 1 and 500 miles")
    v = _vehicle(vin)
    result = {"vin": vin.upper(),
              "vehicle": f"{v.get('year','')} {v.get('make','')} {v.get('model','')}".strip(),
              "your_price": v.get("price") or None,
              "generated_at": datetime.now().isoformat()}

    if MARKETCHECK_KEY:
        try:
            data = _marketcheck_comps(v, radius=radius, zip_code=zip_code)
            stats = (data.get("stats") or {}).get("price") or {}
            listings = data.get("listings") or []
            result.update({
                "source": "marketcheck",
                "comp_count": data.get("num_found", len(listings)),
                "market": {"mean": stats.get("mean"), "median": stats.get("median"),
                           "min": stats.get("min"), "max": stats.get("max")},
                "sample_comps": [
                    {"title": l.get("heading"), "price": l.get("price"),
                     "miles": l.get("miles"), "dealer": (l.get("dealer") or {}).get("name"),
                     "city": (l.get("dealer") or {}).get("city")}
                    for l in listings[:8]
                ],
            })
            return result
        except Exception as e:
            result["marketcheck_error"] = str(e)

    # Fallback: comps from our own inventory (honest label, never presented as market truth)
    conn = _db()
    comps = conn.execute(
        """SELECT price FROM vehicles
           WHERE make=? AND model=? AND vin != ? AND price != '' AND price IS NOT NULL""",
        (v.get("make") or "", v.get("model") or "", vin.upper())).fetchall()
    conn.close()
    prices = []
    for c in comps:
        try:
            prices.append(float(str(c["price"]).replace("$", "").replace(",", "")))
        except ValueError:
            pass
    result.update({
        "source": "local_inventory_estimate",
        "disclaimer": "NOT market data — median of your own inventory for this make/model. "
                      "Configure MARKETCHECK_API_KEY for true market comps.",
        "comp_count": len(prices),
        "market": {"median": statistics.median(prices) if prices else None,
                   "min": min(prices) if prices else None,
                   "max": max(prices) if prices else None},
    })
    return result


# =============================================================================
# C2 — AGGREGATION (LEGAL SOURCES ONLY)
# =============================================================================
@router.get("/api/intel/{vin}/aggregate")
async def vehicle_intel(vin: str):
    """Aggregated vehicle intelligence from legal/public sources:
    NHTSA decode + NHTSA recalls + EPA fuel economy + (Marketcheck if keyed)."""
    v = await run_in_threadpool(_vehicle, vin)
    vin_u = vin.upper()
    out = {"vin": vin_u, "sources_used": [], "generated_at": datetime.now().isoformat()}

    # 1. NHTSA vPIC decode (public)
    try:
        d = await run_in_threadpool(
            _fetch_json, f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin_u}?format=json"
        )
        row = (d.get("Results") or [{}])[0]
        out["decode"] = {k: row.get(k) for k in
                         ["ModelYear", "Make", "Model", "Trim", "EngineModel", "EngineCylinders",
                          "DisplacementL", "TransmissionStyle", "DriveType", "FuelTypePrimary",
                          "BodyClass", "PlantCountry"] if row.get(k)}
        out["sources_used"].append("nhtsa_vpic")
    except Exception as e:
        out["decode_error"] = str(e)

    year = v.get("year") or out.get("decode", {}).get("ModelYear", "")
    make = v.get("make") or out.get("decode", {}).get("Make", "")
    model = v.get("model") or out.get("decode", {}).get("Model", "")

    # 2. NHTSA recalls (public)
    try:
        if year and make and model:
            rc = await run_in_threadpool(
                _fetch_json,
                "https://api.nhtsa.gov/recalls/recallsByVehicle?"
                + urllib.parse.urlencode({"make": make, "model": model, "modelYear": year}),
            )
            recalls = rc.get("results") or []
            out["recalls"] = {"count": len(recalls),
                              "items": [{"campaign": r.get("NHTSACampaignNumber"),
                                         "component": r.get("Component"),
                                         "summary": (r.get("Summary") or "")[:240]}
                                        for r in recalls[:10]]}
            out["sources_used"].append("nhtsa_recalls")
    except Exception as e:
        out["recalls_error"] = str(e)

    # 3. EPA fuel economy (public)
    try:
        if year and make and model:
            fuel_economy = await run_in_threadpool(_epa_fuel_economy, year, make, model)
            if fuel_economy:
                out["fuel_economy"] = fuel_economy
                out["sources_used"].append("epa_fueleconomy")
    except Exception as e:
        out["fuel_economy_error"] = str(e)

    # 4. Marketcheck pricing (licensed, when keyed)
    if MARKETCHECK_KEY:
        try:
            mp = await run_in_threadpool(market_pricing, vin)
            out["market_pricing"] = mp.get("market")
            out["sources_used"].append("marketcheck")
        except Exception as e:
            out["market_pricing_error"] = str(e)

    # 5. Black Book valuation (licensed, when keyed)
    if os.getenv("BLACKBOOK_API_TOKEN", ""):
        try:
            bb = await run_in_threadpool(blackbook_valuation, vin)
            out["blackbook"] = {k: bb.get(k) for k in ("wholesale", "retail", "trade_in", "as_of")}
            out["sources_used"].append("blackbook")
        except Exception as e:
            out["blackbook_error"] = str(e)

    # 6. AutoCheck history (licensed, when keyed)
    if os.getenv("AUTOCHECK_API_KEY", "") and os.getenv("AUTOCHECK_API_SECRET", ""):
        try:
            ac = await run_in_threadpool(autocheck_history, vin)
            out["autocheck"] = ac.get("report")
            out["sources_used"].append("autocheck")
        except Exception as e:
            out["autocheck_error"] = str(e)

    out["legal_note"] = ("Aggregation uses public US-government APIs and licensed data only. "
                         "No scraping of listing sites whose terms prohibit it.")
    return out


# =============================================================================
# C3 — AUCTION FRAMEWORK (pluggable, dark until credentialed)
# =============================================================================
class AuctionAdapter:
    """Base adapter. Subclasses define name, env credential keys, and capabilities.
    An adapter is DARK (inactive) until ALL its env credentials are present —
    no fake data, no pretend bids, ever (anti-fraud covenant)."""
    name = "base"
    display = "Base"
    cred_envs: list = []
    capabilities = ["search", "watch", "proxy_bid"]
    onboarding = ""

    def creds(self):
        return {e: os.getenv(e, "") for e in self.cred_envs}

    def is_live(self):
        return bool(self.cred_envs) and all(self.creds().values())

    def status(self):
        return {"provider": self.name, "display": self.display,
                "live": self.is_live(),
                "missing_credentials": [e for e, v in self.creds().items() if not v],
                "capabilities": self.capabilities,
                "onboarding": self.onboarding}

    # Real implementations are added per-provider once credentialed.
    def search(self, **kw):
        raise HTTPException(503, f"{self.display} adapter is dark — credentials not configured. "
                                 f"Missing: {[e for e, v in self.creds().items() if not v]}")

    bid = watch = search


class ManheimAdapter(AuctionAdapter):
    name = "manheim"
    display = "Manheim (Cox Automotive)"
    cred_envs = ["MANHEIM_CLIENT_ID", "MANHEIM_CLIENT_SECRET"]
    onboarding = ("Requires a Manheim/Cox Automotive developer agreement + dealer auction access "
                  "(5 million accounts: developer.manheim.com). OAuth2 client credentials.")


class AdesaAdapter(AuctionAdapter):
    name = "adesa"
    display = "ADESA (OPENLANE)"
    cred_envs = ["ADESA_API_KEY", "ADESA_DEALER_ID"]
    onboarding = "Requires OPENLANE dealer registration + API enablement through your rep."


class CopartAdapter(AuctionAdapter):
    name = "copart"
    display = "Copart"
    cred_envs = ["COPART_USERNAME", "COPART_API_TOKEN"]
    onboarding = ("Copart has no public API; access is via their licensed broker/registered-buyer "
                  "programs. This adapter stays dark unless you have a licensed integration.")


_ADAPTERS = {a.name: a for a in [ManheimAdapter(), AdesaAdapter(), CopartAdapter()]}


@router.get("/api/auctions/providers")
def auction_providers():
    return {"providers": [a.status() for a in _ADAPTERS.values()],
            "note": "Adapters activate automatically once their env credentials are set. "
                    "Dark adapters refuse all operations — no simulated data."}


@router.get("/api/auctions/{provider}/status")
def auction_status(provider: str):
    a = _ADAPTERS.get(provider.lower())
    if not a:
        raise HTTPException(404, f"Unknown provider. Available: {list(_ADAPTERS)}")
    return a.status()


@router.get("/api/auctions/{provider}/search")
def auction_search(provider: str, make: str = "", model: str = "",
                         year_min: int = 0, year_max: int = 0):
    a = _ADAPTERS.get(provider.lower())
    if not a:
        raise HTTPException(404, f"Unknown provider. Available: {list(_ADAPTERS)}")
    return a.search(make=make, model=model, year_min=year_min, year_max=year_max)


# =============================================================================
# C4 — BLACK BOOK VALUATION (native adapter; licensed API, dark until keyed)
# =============================================================================
BLACKBOOK_TOKEN = os.getenv("BLACKBOOK_API_TOKEN", "")
BLACKBOOK_HOST = os.getenv("BLACKBOOK_HOST", "https://service.blackbookcloud.com")


@router.get("/api/valuation/{vin}/blackbook")
def blackbook_valuation(vin: str, mileage: int = 0, zip_code: str = ""):
    """Black Book wholesale/retail valuation — UsedCarValues by VIN.
    Activates when BLACKBOOK_API_TOKEN is set (license at blackbook.com/api)."""
    vin = vin.upper()
    if not BLACKBOOK_TOKEN:
        raise HTTPException(503, "Black Book not credentialed. Set BLACKBOOK_API_TOKEN "
                                 "(licensed at blackbookcloud.com — Dealer/Lender Data License).")
    v = _vehicle(vin)
    params = {"customerid": os.getenv("BLACKBOOK_CUSTOMER_ID", "lotvue360")}
    if mileage or v.get("mileage"):
        params["mileage"] = mileage or v.get("mileage")
    url = (f"{BLACKBOOK_HOST}/UsedCarWS/UsedCarWS/UsedVehicle/VIN/{vin}"
           f"?{urllib.parse.urlencode(params)}")
    try:
        data = _fetch_json(url, headers={"Authorization": f"Bearer {BLACKBOOK_TOKEN}",
                                         "Accept": "application/json"}, timeout=25)
    except Exception as e:
        raise HTTPException(502, f"Black Book API error: {e}")
    # normalize the used_vehicles payload to a stable shape
    try:
        uv = data["used_vehicles"]["used_vehicle_list"][0]
        out = {
            "vin": vin, "source": "blackbook",
            "wholesale": {"clean": uv.get("adjusted_whole_clean"),
                          "average": uv.get("adjusted_whole_avg"),
                          "rough": uv.get("adjusted_whole_rough")},
            "retail": {"clean": uv.get("adjusted_retail_clean"),
                       "average": uv.get("adjusted_retail_avg"),
                       "rough": uv.get("adjusted_retail_rough")},
            "trade_in": {"clean": uv.get("adjusted_tradein_clean"),
                         "average": uv.get("adjusted_tradein_avg")},
            "as_of": uv.get("publish_date"),
        }
    except (KeyError, IndexError, TypeError):
        out = {"vin": vin, "source": "blackbook", "raw": data}
    return out


# =============================================================================
# C5 — AUTOCHECK VEHICLE HISTORY (native adapter; Experian license, dark until keyed)
# =============================================================================
AUTOCHECK_KEY = os.getenv("AUTOCHECK_API_KEY", "")
AUTOCHECK_SECRET = os.getenv("AUTOCHECK_API_SECRET", "")
AUTOCHECK_HOST = os.getenv("AUTOCHECK_HOST", "https://api.experian.com/automotive/autocheck/v1")


@router.get("/api/history/{vin}/autocheck")
def autocheck_history(vin: str):
    """AutoCheck vehicle history score + records via Experian Automotive API.
    Activates when AUTOCHECK_API_KEY/SECRET are set (developer.experian.com)."""
    vin = vin.upper()
    if not (AUTOCHECK_KEY and AUTOCHECK_SECRET):
        raise HTTPException(503, "AutoCheck not credentialed. Set AUTOCHECK_API_KEY + "
                                 "AUTOCHECK_API_SECRET (Experian Automotive developer agreement).")
    # OAuth2 client-credentials → vehicle history
    try:
        tok_req = urllib.request.Request(
            "https://api.experian.com/oauth2/v1/token",
            data=urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": AUTOCHECK_KEY, "client_secret": AUTOCHECK_SECRET,
            }).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with urllib.request.urlopen(tok_req, timeout=20) as r:
            token = json.loads(r.read())["access_token"]
        data = _fetch_json(f"{AUTOCHECK_HOST}/summary/{vin}",
                           headers={"Authorization": f"Bearer {token}",
                                    "Accept": "application/json"}, timeout=25)
    except Exception as e:
        raise HTTPException(502, f"AutoCheck API error: {e}")
    return {"vin": vin, "source": "autocheck", "report": data}


# =============================================================================
# C6 — REVERSE VIN: license-plate → VIN (licensed lookup, dark until keyed)
#       (VIN barcode scanning runs CLIENT-SIDE in the /mobile app via ZXing)
# =============================================================================
PLATE2VIN_KEY = os.getenv("PLATE2VIN_API_KEY", "")
PLATE2VIN_HOST = os.getenv("PLATE2VIN_HOST", "https://platetovin.com/api/convert")


@router.get("/api/vin/from-plate")
def vin_from_plate(plate: str, state: str):
    """Reverse VIN from a license plate (licensed plate-to-VIN provider).
    Activates when PLATE2VIN_API_KEY is set. The mobile app's barcode scanner
    covers the no-credential path (door-jamb VIN barcode, client-side ZXing)."""
    if not PLATE2VIN_KEY:
        raise HTTPException(503, "Plate-to-VIN not credentialed. Set PLATE2VIN_API_KEY "
                                 "(platetovin.com or a licensed equivalent). The /mobile "
                                 "app's VIN barcode scanner works without credentials.")
    try:
        req = urllib.request.Request(
            f"{PLATE2VIN_HOST}/{state.upper()}/{urllib.parse.quote(plate.upper())}",
            headers={"Authorization": PLATE2VIN_KEY, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as e:
        raise HTTPException(502, f"Plate-to-VIN provider error: {e}")
    vin = (data.get("vin") or {}).get("vin") if isinstance(data.get("vin"), dict) else data.get("vin")
    return {"plate": plate.upper(), "state": state.upper(), "vin": vin, "raw": data}


@router.get("/api/intel/status")
def intel_status():
    """One honest pane: which intelligence integrations are live vs dark."""
    return {
        "marketcheck": {"live": bool(MARKETCHECK_KEY), "env": "MARKETCHECK_API_KEY"},
        "blackbook": {"live": bool(BLACKBOOK_TOKEN), "env": "BLACKBOOK_API_TOKEN"},
        "autocheck": {"live": bool(AUTOCHECK_KEY and AUTOCHECK_SECRET),
                      "env": "AUTOCHECK_API_KEY + AUTOCHECK_API_SECRET"},
        "plate_to_vin": {"live": bool(PLATE2VIN_KEY), "env": "PLATE2VIN_API_KEY"},
        "vin_barcode_scan": {"live": True, "note": "client-side in /mobile, no credentials needed"},
        "nhtsa_decode_recalls": {"live": True, "note": "public US-gov API"},
        "epa_fuel_economy": {"live": True, "note": "public US-gov API"},
        "auctions": {"live": False, "providers": list(_ADAPTERS),
                     "note": "dark until per-provider credentials"},
        "policy": "Dark integrations refuse with 503 — no simulated data, ever.",
    }


# =============================================================================
# REGISTRATION
# =============================================================================
def register_intelligence(app):
    app.include_router(router)
    print("[C-Intelligence] registered: Marketcheck pricing (+local fallback), "
          "legal aggregation (vPIC/recalls/EPA), auction framework (3 dark adapters)")
