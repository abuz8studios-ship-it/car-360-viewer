"""
LotVue Pro — Market Pricing Intelligence
Estimates market value based on vehicle specs.
Uses NHTSA data + statistical pricing model.
Future: web scraping of marketplace listings for real comps.
"""
# Base price estimates by body class (rough market averages for used vehicles)
BASE_PRICES = {
    "sedan": 18000,
    "suv": 25000,
    "truck": 28000,
    "pickup": 28000,
    "coupe": 22000,
    "convertible": 26000,
    "van": 20000,
    "wagon": 17000,
    "hatchback": 16000,
    "crossover": 23000,
}

# Make premium multipliers
MAKE_MULTIPLIERS = {
    "mercedes-benz": 1.6, "bmw": 1.5, "audi": 1.45, "lexus": 1.4,
    "porsche": 2.2, "land rover": 1.7, "jaguar": 1.3, "infiniti": 1.2,
    "acura": 1.15, "volvo": 1.2, "cadillac": 1.25, "lincoln": 1.15,
    "tesla": 1.5, "genesis": 1.3,
    "toyota": 1.1, "honda": 1.05, "subaru": 1.05, "mazda": 1.0,
    "ford": 0.95, "chevrolet": 0.95, "gmc": 1.05, "ram": 1.0,
    "dodge": 0.9, "jeep": 1.05, "hyundai": 0.9, "kia": 0.88,
    "nissan": 0.85, "volkswagen": 0.95, "mitsubishi": 0.8,
    "chrysler": 0.8, "buick": 0.85, "fiat": 0.7,
}

# Year depreciation curve (years old → multiplier)
def year_depreciation(years_old: int) -> float:
    """Standard depreciation curve for used vehicles."""
    if years_old <= 0:
        return 1.0
    elif years_old == 1:
        return 0.80
    elif years_old == 2:
        return 0.72
    elif years_old == 3:
        return 0.65
    elif years_old == 4:
        return 0.58
    elif years_old == 5:
        return 0.52
    elif years_old <= 7:
        return 0.52 - (years_old - 5) * 0.05
    elif years_old <= 10:
        return 0.42 - (years_old - 7) * 0.04
    else:
        return max(0.15, 0.30 - (years_old - 10) * 0.03)


def mileage_adjustment(mileage: int) -> float:
    """Adjust price based on mileage (average is ~12k/year)."""
    if mileage <= 0:
        return 1.0
    if mileage < 30000:
        return 1.08
    elif mileage < 50000:
        return 1.03
    elif mileage < 75000:
        return 1.0
    elif mileage < 100000:
        return 0.92
    elif mileage < 130000:
        return 0.82
    elif mileage < 160000:
        return 0.70
    else:
        return max(0.45, 0.70 - (mileage - 160000) / 100000 * 0.25)


def estimate_market_price(vehicle_info: dict) -> dict:
    """
    Estimate market value for a vehicle based on its specs.
    Returns market average, suggested range, and competitive rating.
    """
    from datetime import datetime
    current_year = datetime.now().year

    year = int(vehicle_info.get("year", 0) or 0)
    make = (vehicle_info.get("make", "") or "").lower()
    model = (vehicle_info.get("model", "") or "").lower()
    body = (vehicle_info.get("body_style", "") or "").lower()
    mileage_str = str(vehicle_info.get("mileage", "0") or "0").replace(",", "").strip()
    try:
        mileage_val = int(float(mileage_str))
    except (ValueError, TypeError, OverflowError):
        mileage_val = 0

    asking_price_str = str(vehicle_info.get("price", "0") or "0").replace(",", "").replace("$", "").strip()
    try:
        asking_price = float(asking_price_str)
    except (ValueError, TypeError, OverflowError):
        asking_price = 0

    # Determine body class
    body_key = "sedan"  # default
    for key in BASE_PRICES:
        if key in body or key in model:
            body_key = key
            break
    # Truck/SUV detection from model name
    suv_keywords = ["suv", "explorer", "tahoe", "suburban", "4runner", "highlander", "pilot",
                     "rav4", "cr-v", "crv", "equinox", "escape", "edge", "blazer", "telluride",
                     "sorento", "tucson", "santa fe", "pathfinder", "murano", "rogue",
                     "wrangler", "grand cherokee", "cherokee", "compass", "gladiator"]
    truck_keywords = ["f-150", "f150", "f-250", "f250", "silverado", "sierra", "ram", "tundra",
                       "tacoma", "frontier", "colorado", "ranger", "titan", "ridgeline"]
    for kw in suv_keywords:
        if kw in model:
            body_key = "suv"
            break
    for kw in truck_keywords:
        if kw in model:
            body_key = "truck"
            break

    base = BASE_PRICES.get(body_key, 18000)

    # Apply multipliers
    make_mult = MAKE_MULTIPLIERS.get(make, 1.0)
    years_old = max(0, current_year - year) if year > 1990 else 10
    year_mult = year_depreciation(years_old)
    mile_mult = mileage_adjustment(mileage_val)

    estimated = base * make_mult * year_mult * mile_mult

    # Create range (±12%)
    low = int(estimated * 0.88)
    high = int(estimated * 1.12)
    market_avg = int(estimated)

    # Competitive rating
    if asking_price > 0:
        ratio = asking_price / max(market_avg, 1)
        if ratio < 0.90:
            rating = "UNDERPRICED"
            rating_color = "#e74c3c"
        elif ratio < 0.97:
            rating = "GREAT DEAL"
            rating_color = "#27ae60"
        elif ratio <= 1.05:
            rating = "FAIR"
            rating_color = "#2E75B6"
        elif ratio <= 1.15:
            rating = "ABOVE MARKET"
            rating_color = "#e67e22"
        else:
            rating = "HIGH"
            rating_color = "#e74c3c"
    else:
        rating = "SET PRICE"
        rating_color = "#6c757d"

    return {
        "market_average": market_avg,
        "suggested_low": low,
        "suggested_high": high,
        "asking_price": int(asking_price) if asking_price else None,
        "rating": rating,
        "rating_color": rating_color,
        "factors": {
            "base_price": base,
            "body_type": body_key,
            "make_premium": round(make_mult, 2),
            "year_depreciation": round(year_mult, 2),
            "mileage_factor": round(mile_mult, 2),
            "years_old": years_old,
        }
    }
