from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

VIN = "1HGCM82633A004352"


def test_analytics_beacon_is_bounded_and_only_accepts_active_vehicle_events(monkeypatch) -> None:
    monkeypatch.setattr(main, "db_get_vehicle", lambda vin: {"vin": vin, "status": "active"})
    client = TestClient(main.app)

    accepted = client.post("/api/analytics/event", json={
        "vin": VIN,
        "event_type": "tab_switch",
        "data": {"tab": "<script>"},
        "ts": 123,
    })
    assert accepted.status_code == 200

    unsupported = client.post("/api/analytics/event", json={"vin": VIN, "event_type": "arbitrary"})
    assert unsupported.status_code == 400

    monkeypatch.setattr(main, "db_get_vehicle", lambda vin: {"vin": vin, "status": "deleted"})
    inactive = client.post("/api/analytics/event", json={"vin": VIN, "event_type": "viewer_open"})
    assert inactive.status_code == 404

    oversized = client.post("/api/analytics/event", content=b"{" + b" " * 9000 + b"}")
    assert oversized.status_code == 413
