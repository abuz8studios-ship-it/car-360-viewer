from __future__ import annotations

import asyncio
import io
import json
import sys
import threading
from pathlib import Path

from starlette.datastructures import UploadFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import main  # noqa: E402

VIN = "1HGCM82633A004352"


def test_exterior_upload_uses_module_threadpool_without_local_name_shadowing(
    monkeypatch, tmp_path: Path
) -> None:
    vehicle_dir = tmp_path / VIN
    exterior_dir = vehicle_dir / "exterior"

    monkeypatch.setattr(
        main, "_require_vehicle_access", lambda *_args, **_kwargs: ("dealer@example.test", None)
    )

    def prepare_vehicle(*_args, **_kwargs):
        exterior_dir.mkdir(parents=True, exist_ok=True)
        return vehicle_dir

    async def read_upload(_upload, _limit):
        return b"validated-image-bytes"

    monkeypatch.setattr(main, "_prepare_vehicle_upload", prepare_vehicle)
    monkeypatch.setattr(main, "_read_upload_limited", read_upload)
    monkeypatch.setattr(main, "get_dealer_showroom", lambda _email: None)
    monkeypatch.setattr(
        main,
        "process_exterior_photo",
        lambda content, path, _showroom: path.write_bytes(content),
    )
    monkeypatch.setattr(main, "generate_thumbnail", lambda *_args: None)
    monkeypatch.setattr(main, "db_update_counts", lambda _vin: None)

    result = asyncio.run(
        main.upload_exterior_photo(
            object(), VIN, UploadFile(filename="frame.jpg", file=io.BytesIO(b"upload")), "0"
        )
    )

    assert result == {"status": "ok", "vin": VIN, "filename": "frame_0001.jpg", "index": 1}
    assert (exterior_dir / "frame_0001.jpg").read_bytes() == b"validated-image-bytes"


def test_comfyui_status_uses_module_threadpool_without_local_name_shadowing(monkeypatch) -> None:
    from ai_studio import background_studio

    monkeypatch.setattr(main, "_authenticated_user_email", lambda _request: "dealer@example.test")
    monkeypatch.setattr(main, "_comfyui_is_available", lambda: True)
    monkeypatch.setattr(background_studio, "comfyui_scene_workflow_configured", lambda: True)

    assert asyncio.run(main.comfyui_status(object())) == {
        "available": True,
        "scene_workflow_configured": True,
        "scene_generation_available": True,
    }


def test_public_lead_submission_uses_module_threadpool_without_local_name_shadowing(
    monkeypatch,
) -> None:
    from ai_studio import lead_engine

    async def read_body(_request, _limit):
        return json.dumps({"name": "Buyer", "email": "buyer@example.test"}).encode()

    monkeypatch.setattr(main, "_check_public_lead_rate", lambda _request: None)
    monkeypatch.setattr(main, "_read_limited_body", read_body)
    monkeypatch.setattr(main, "_insert_public_lead", lambda *_args: 41)
    monkeypatch.setattr(lead_engine, "score_lead", lambda _lead, _vehicle: {"grade": "A"})
    monkeypatch.setattr(lead_engine, "generate_lead_response", lambda *_args: "Thanks for asking.")
    monkeypatch.setattr(lead_engine, "schedule_follow_up_sequence", lambda *_args: [])

    result = asyncio.run(main.submit_public_lead(object()))

    assert result == {
        "status": "ok",
        "lead_id": 41,
        "message": "Thanks — the dealer will reach out shortly.",
    }


def test_distribution_imports_offload_database_auth(monkeypatch) -> None:
    import b_distribution

    loop_thread = threading.get_ident()
    auth_threads = []
    import_threads = []

    def fake_actor_email(_request):
        auth_threads.append(threading.get_ident())
        return "dealer@example.test"

    def fake_import(*_args, **_kwargs):
        import_threads.append(threading.get_ident())
        return {"status": "ok"}

    monkeypatch.setattr(b_distribution, "_actor_email", fake_actor_email)
    monkeypatch.setattr(b_distribution, "_import_rows", fake_import)
    monkeypatch.setattr(b_distribution, "_consume_dms_csv", fake_import)

    async def run_imports():
        csv_upload = UploadFile(
            filename="inventory.csv",
            file=io.BytesIO(b"vin,year,make,model\n1HGCM82633A004352,2024,Honda,Accord\n"),
        )
        dms_upload = UploadFile(
            filename="inventory.csv",
            file=io.BytesIO(b"vin,year,make,model\n1HGCM82633A004352,2024,Honda,Accord\n"),
        )
        await b_distribution.import_csv(object(), csv_upload)
        await b_distribution.dms_import("dealercenter", object(), dms_upload)

    asyncio.run(run_imports())

    assert len(auth_threads) == 2
    assert len(import_threads) == 2
    assert all(thread_id != loop_thread for thread_id in auth_threads + import_threads)
