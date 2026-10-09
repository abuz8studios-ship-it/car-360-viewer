"""URL validation and normalization for buyer-facing vehicle videos."""

import re
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import HTTPException


def normalize_video_source(video_url: str, video_type: str, vin: str) -> tuple[str, str]:
    """Canonicalize supported buyer video sources; reject unsafe/unsupported URLs."""
    value = str(video_url or "").strip()
    requested_type = str(video_type or "").strip().lower()
    vin = str(vin or "").strip().upper()
    if not value:
        return "", ""
    if not re.fullmatch(r"[A-Z0-9_-]{1,32}", vin):
        raise HTTPException(status_code=400, detail="Invalid vehicle identifier")
    if len(value) > 2048 or any(ord(char) < 0x20 or char.isspace() for char in value):
        raise HTTPException(status_code=400, detail="Video URL is invalid or too long")
    if "\\" in value:
        raise HTTPException(status_code=400, detail="Video URL contains an invalid path separator")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid video URL") from exc

    youtube_hosts = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
    vimeo_hosts = {"vimeo.com", "www.vimeo.com", "player.vimeo.com"}
    provider_hosts = youtube_hosts | vimeo_hosts
    try:
        decoded_path = unquote(parsed.path, errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid video URL path") from exc
    path_parts = [part for part in decoded_path.split("/") if part]
    if ("\\" in decoded_path or any(ord(char) < 0x20 for char in decoded_path)
            or any(part in {".", ".."} for part in path_parts)):
        raise HTTPException(status_code=400, detail="Video URL contains an invalid path")

    # Provider inputs are always canonicalized to HTTPS URLs on fixed trusted
    # hosts. Reject credentials, non-HTTPS schemes, and custom ports to avoid
    # accepting deceptive authority strings.
    if host in provider_hosts or requested_type in {"youtube", "vimeo"}:
        if (parsed.scheme.lower() != "https" or host not in provider_hosts
                or parsed.username or parsed.password or port not in {None, 443}):
            raise HTTPException(status_code=400, detail="Use a secure, valid YouTube or Vimeo URL")

    if host in youtube_hosts or requested_type == "youtube":
        video_id = ""
        if host in {"youtu.be", "www.youtu.be"} and path_parts:
            video_id = path_parts[0]
        elif host in youtube_hosts:
            video_id = parse_qs(parsed.query, keep_blank_values=True).get("v", [""])[0]
            if not video_id and len(path_parts) >= 2 and path_parts[0] in {"embed", "shorts", "live"}:
                video_id = path_parts[1]
        if host not in youtube_hosts or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise HTTPException(status_code=400, detail="Enter a valid YouTube video URL")
        return f"https://www.youtube.com/watch?v={video_id}", "youtube"

    if host in vimeo_hosts or requested_type == "vimeo":
        video_id = path_parts[-1] if path_parts else ""
        if host not in vimeo_hosts or not re.fullmatch(r"[0-9]{1,20}", video_id):
            raise HTTPException(status_code=400, detail="Enter a valid Vimeo video URL")
        return f"https://vimeo.com/{video_id}", "vimeo"

    if requested_type not in {"", "upload", "mp4", "video"}:
        raise HTTPException(status_code=400, detail="Video type must be YouTube, Vimeo, or a direct video URL")
    allowed_suffixes = {".mp4", ".m4v", ".webm"}
    if not parsed.scheme and not parsed.netloc:
        safe_path = decoded_path
        segments = safe_path.split("/")
        published_video_dirs = {"walkaround", "video", "videos", "commercial", "video_ads", "exports"}
        if (parsed.query or parsed.fragment or "%" in parsed.path
                or len(segments) < 5 or segments[1] != "storage" or segments[2] != vin
                or segments[3] not in published_video_dirs
                or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in segments[1:])
                or PurePosixPath(safe_path).suffix.lower() not in allowed_suffixes):
            raise HTTPException(status_code=400, detail="Direct videos must be published under this vehicle's storage path")
        return safe_path, "upload"
    if (parsed.scheme.lower() != "https" or not host or parsed.username or parsed.password
            or port not in {None, 443} or PurePosixPath(decoded_path).suffix.lower() not in allowed_suffixes):
        raise HTTPException(status_code=400, detail="Direct video URL must be an HTTPS MP4, M4V, or WebM")
    return value, "upload"
