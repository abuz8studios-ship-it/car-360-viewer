from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.video_urls import normalize_video_source

VIN = "1HGCM82633A004352"


@pytest.mark.parametrize(
    ("url", "kind", "expected"),
    [
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
            "",
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ),
        (
            "https://youtu.be/dQw4w9WgXcQ?t=20",
            "youtube",
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ),
        (
            "https://youtube.com/shorts/dQw4w9WgXcQ",
            "",
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ),
        (
            "https://player.vimeo.com/video/123456789",
            "",
            ("https://vimeo.com/123456789", "vimeo"),
        ),
        (
            "https://media.example.test/cars/walkaround.mp4?token=abc",
            "",
            ("https://media.example.test/cars/walkaround.mp4?token=abc", "upload"),
        ),
        (
            f"/storage/{VIN}/walkaround/clip.webm",
            "upload",
            (f"/storage/{VIN}/walkaround/clip.webm", "upload"),
        ),
        ("   ", "not-a-type", ("", "")),
    ],
)
def test_normalizes_supported_video_sources(url: str, kind: str, expected: tuple[str, str]) -> None:
    assert normalize_video_source(url, kind, VIN) == expected


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("http://youtube.com/watch?v=dQw4w9WgXcQ", ""),
        ("//youtube.com/watch?v=dQw4w9WgXcQ", ""),
        ("https://youtube.com.attacker.test/watch?v=dQw4w9WgXcQ", ""),
        ("https://user@youtube.com/watch?v=dQw4w9WgXcQ", ""),
        ("https://youtube.com:8443/watch?v=dQw4w9WgXcQ", ""),
        ("https://youtube.com/watch?v=short", ""),
        ("https://vimeo.com/12abc", ""),
        ("http://media.example.test/clip.mp4", "upload"),
        ("https://media.example.test/clip.avi", "upload"),
        ("https://media.example.test/clip.mp4@evil.test", "upload"),
        ("/storage/1HGCM82633A004352/walkaround/../secret.mp4", "upload"),
        ("/storage/1HGCM82633A004352/walkaround/%2e%2e/secret.mp4", "upload"),
        ("/storage/1HGCM82633A004352/raw/clip.mp4", "upload"),
        ("/storage/OTHER123456789/walkaround/clip.mp4", "upload"),
        ("https://[invalid/clip.mp4", "upload"),
        ("https://media.example.test/clip.mp4\n.evil", "upload"),
    ],
)
def test_rejects_invalid_or_unsafe_video_sources(url: str, kind: str) -> None:
    with pytest.raises(HTTPException):
        normalize_video_source(url, kind, VIN)


def test_direct_url_can_be_inferred_without_a_type() -> None:
    assert normalize_video_source("https://cdn.example.test/car.m4v", "", VIN) == (
        "https://cdn.example.test/car.m4v",
        "upload",
    )


def test_local_paths_require_a_safe_vehicle_id() -> None:
    with pytest.raises(HTTPException):
        normalize_video_source("/storage/VIN/video.mp4", "upload", "../VIN")
