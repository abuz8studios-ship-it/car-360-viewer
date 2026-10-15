"""Free tier = LOTVUE_FREE_VEHICLES vehicles for the life of the account.

The isolated app in conftest runs with LOTVUE_FREE_VEHICLES=2 and payments on.
Deleting a vehicle (soft or hard) never frees a slot; re-adding a VIN the
dealer already had never costs one; bulk CSV import stops at the allowance.
"""
from conftest import register, signed_webhook

META = {"year": "2019", "make": "Honda", "model": "Civic"}  # skip NHTSA lookup


def _vins(series: int) -> list[str]:
    """Three VINs unique to one test (the isolated app is shared per module)."""
    return [f"2HGFC2F69KH6{series}00{i}" for i in (11, 12, 13)]


def _create(client, vin):
    return client.post("/api/create-vehicle", data={"vin": vin, **META})


def test_deleting_vehicles_never_frees_a_free_slot(client):
    register(client, "lifetime@example.com")
    VINS = _vins(1)
    assert _create(client, VINS[0]).status_code == 200
    assert _create(client, VINS[1]).status_code == 200

    assert client.delete(f"/api/vehicle/{VINS[0]}").status_code == 200   # soft delete
    r = _create(client, VINS[2])
    assert r.status_code == 402
    assert r.json()["detail"]["entitlement"]["vehicles_used"] == 2

    assert client.delete("/api/vehicles/clear-all").status_code == 200    # hard delete
    assert _create(client, VINS[2]).status_code == 402

    # a VIN this account already had is a re-shoot, not a new free vehicle
    assert _create(client, VINS[1]).status_code == 200


def test_paid_plan_lifts_the_lifetime_limit(client, app_module):
    email = "upgrade@example.com"
    register(client, email)
    VINS = _vins(2)
    assert _create(client, VINS[0]).status_code == 200
    assert _create(client, VINS[1]).status_code == 200
    assert _create(client, VINS[2]).status_code == 402
    ev = {"id": "evt_free_tier_upgrade", "type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "payment_status": "paid", "client_reference_id": email,
        "customer": "cus_free_tier", "subscription": "sub_free_tier"}}}
    assert signed_webhook(client, ev).json()["outcome"] == "activated"
    assert _create(client, VINS[2]).status_code == 200


def test_csv_import_stops_at_the_free_allowance(client):
    register(client, "importer@example.com")
    VINS = _vins(3)
    csv = "vin,year,make,model\n" + "".join(f"{v},2019,Honda,Civic\n" for v in VINS)
    r = client.post("/api/import/csv", files={"file": ("inventory.csv", csv, "text/csv")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 2
    assert [e["error"] for e in body["errors"]] == ["free vehicle limit reached"]
    assert body["errors"][0]["vin"] == VINS[2]
