"""Canonical, packaged vehicle-background catalog shared by all studio paths."""
from __future__ import annotations

import re
from pathlib import Path

from app_paths import BASE_DIR


STATIC_BACKGROUND_DIR = BASE_DIR / "static" / "backgrounds"
_THUMBNAIL_DIR = STATIC_BACKGROUND_DIR / "thumbs"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# The older Background Studio scene IDs remain accepted at its API boundary,
# but every scene resolves to the same packaged asset set used by the 360
# renderer and Photo Studio.
SCENE_BACKGROUND_SLUGS = {
    "luxury-showroom": "showroom_lux",
    "outdoor-mountain": "outdoor_mountain",
    "urban-night": "outdoor_night_city",
    "clean-studio": "studio_white",
    "desert-sunset": "outdoor_desert",
    "beach-coast": "outdoor_coast",
    "dealer-lot": "showroom_classic",
    "garage-dark": "studio_dark",
}


def background_category(slug: str) -> str:
    for prefix, category in (
        ("studio_", "studio"),
        ("showroom_", "showroom"),
        ("outdoor_", "outdoor"),
        ("seasonal_", "seasonal"),
    ):
        if slug.startswith(prefix):
            return category
    return "studio"


def background_asset_path(slug: str, directory: Path = STATIC_BACKGROUND_DIR) -> Path | None:
    """Resolve one safe JPEG slug and reject symlinks or paths outside the library."""
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        return None
    candidate = directory / f"{slug}.jpg"
    try:
        root = directory.resolve(strict=True)
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve(strict=True)
        if resolved.parent != root or not resolved.is_file():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def list_background_assets(*, include_none: bool = False) -> list[dict]:
    """Return only packaged assets that exist, with working preview URLs."""
    assets: list[dict] = []
    try:
        paths = sorted(
            (path for path in STATIC_BACKGROUND_DIR.glob("*.jpg")
             if path.is_file() and not path.is_symlink()),
            key=lambda path: path.stem,
        )
    except OSError:
        paths = []
    for path in paths:
        slug = path.stem
        if not _SLUG_RE.fullmatch(slug):
            continue
        thumb = _THUMBNAIL_DIR / path.name
        try:
            has_thumb = thumb.is_file() and not thumb.is_symlink()
        except OSError:
            has_thumb = False
        url = f"/static/backgrounds/{path.name}"
        assets.append({
            "name": slug.replace("_", " ").title(),
            "slug": slug,
            "url": url,
            "thumbnail_url": f"/static/backgrounds/thumbs/{path.name}" if has_thumb else url,
            "category": background_category(slug),
            "is_active": 1,
            "is_system": 1,
        })
    if include_none:
        assets.append({
            "name": "Keep photographed background",
            "slug": "none",
            "url": "",
            "thumbnail_url": "",
            "category": "natural",
            "is_active": 1,
            "is_system": 1,
        })
    return assets


def scene_asset_slug(scene_id: str) -> str | None:
    return SCENE_BACKGROUND_SLUGS.get(scene_id)
