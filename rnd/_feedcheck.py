"""Validate feed against Google vehicle-listings spec (required attrs,
fetched 2026-08-12 from developers.google.com/vehicle-listings)."""
import re
import xml.etree.ElementTree as ET
G = "{http://base.google.com/ns/1.0}"
REQ = ["vin", "store_code", "dealership_name", "dealership_address",
       "price", "condition", "make", "model", "year", "mileage"]
t = ET.parse(r'E:\ABU\01_PRODUCTS\360Car-LotVue\rnd\_gate_probe\gfeed.xml')
items = t.getroot().findall(".//item")
print("WELL-FORMED XML, items:", len(items))
bad = 0
for i, it in enumerate(items):
    errs = []
    for a in REQ:
        v = (it.findtext(f"{G}{a}") or "").strip()
        if not v:
            errs.append(f"missing {a}")
    p = (it.findtext(f"{G}price") or "").strip()
    if p and not re.fullmatch(r"[\d.]+ [A-Z]{3}", p):
        errs.append(f"bad price format: {p}")
    y = (it.findtext(f"{G}year") or "").strip()
    if y and not re.fullmatch(r"\d{4}", y):
        errs.append(f"bad year: {y}")
    if not (it.findtext(f"{G}image_link") or "").strip():
        errs.append("missing image_link")
    if it.find(f"{G}vehicle_fulfillment") is None:
        errs.append("missing vehicle_fulfillment")
    if errs:
        bad += 1
        if bad <= 5:
            print(f"  item {i} ({it.findtext(f'{G}id')}): {errs}")
print(f"RESULT: {len(items)-bad}/{len(items)} items pass ALL required-attribute checks")
print("VERDICT:", "VALID" if bad == 0 and items else "INVALID")
