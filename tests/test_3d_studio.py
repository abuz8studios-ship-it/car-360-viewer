"""3D Studio (/api/vehicle/{vin}/generate-3d) against an isolated app copy."""
import io

from PIL import Image

from conftest import register

VIN = "19UDE2F72MA005771"


def _photos():
    def jpeg(shade):
        buf = io.BytesIO()
        Image.new("RGB", (1200, 800), (shade, 90, 120)).save(buf, "JPEG")
        return buf.getvalue()
    return [("photos", (f"p{i}.jpg", jpeg(40 * i), "image/jpeg")) for i in range(4)]


def _gate_passes(monkeypatch):
    import validate_video
    monkeypatch.setattr(validate_video, "validate_dir", lambda d, sample=10: {
        "verdict": "PASS", "reasons": [], "checks": {}, "frames": 4})


def test_3d_studio_starts_a_new_vehicle_for_the_signed_in_dealer(client, app_module, monkeypatch):
    _gate_passes(monkeypatch)
    started = []
    monkeypatch.setattr(app_module, "_submit_background_job",
                        lambda key, fn, *args: started.append(key) or "started")
    register(client, "studio3d@example.com")

    r = client.post(f"/api/vehicle/{VIN}/generate-3d", data={"style": "studio"}, files=_photos())

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "started"
    assert started == [f"gaussian3d:{VIN}"]
    assert app_module.db_get_vehicle(VIN)["user_email"] == "studio3d@example.com"

    client.cookies.clear()
    register(client, "rival@example.com")
    r = client.post(f"/api/vehicle/{VIN}/generate-3d", data={"style": "studio"}, files=_photos())
    assert r.status_code in (403, 404)
    assert started == [f"gaussian3d:{VIN}"]


def test_3d_studio_rejects_a_bad_request_without_creating_a_vehicle(client, app_module, monkeypatch):
    _gate_passes(monkeypatch)
    register(client, "studio3d-bad@example.com")

    r = client.post("/api/vehicle/LV360BADSTYLE01/generate-3d", data={"style": "neon"}, files=_photos())

    assert r.status_code == 422
    assert app_module.db_get_vehicle("LV360BADSTYLE01") is None


def test_3d_studio_passes_the_spin_mode_and_photo_order_to_the_engine(client, app_module, monkeypatch):
    _gate_passes(monkeypatch)
    jobs = []
    monkeypatch.setattr(app_module, "_submit_background_job",
                        lambda key, fn, *args: jobs.append(args) or "started")
    register(client, "studio3d-modes@example.com")

    r = client.post("/api/vehicle/LV360MODES0001/generate-3d", files=_photos(),
                    data={"spin_mode": "photos", "photo_order": "auto"})
    assert r.status_code == 200, r.text
    r = client.post("/api/vehicle/LV360MODES0002/generate-3d", files=_photos())
    assert r.status_code == 200, r.text

    first, default = (args[1] for args in jobs)
    assert first[-4:] == ["--spin-mode", "photos", "--photo-order", "auto"]
    assert default[-4:] == ["--spin-mode", "ai", "--photo-order", "upload"]


def test_3d_studio_rejects_an_unknown_spin_mode_without_creating_a_vehicle(client, app_module, monkeypatch):
    _gate_passes(monkeypatch)
    register(client, "studio3d-badmode@example.com")

    for data in ({"spin_mode": "magic"}, {"photo_order": "random"}):
        r = client.post("/api/vehicle/LV360BADMODE01/generate-3d", data=data, files=_photos())
        assert r.status_code == 422
    assert app_module.db_get_vehicle("LV360BADMODE01") is None
