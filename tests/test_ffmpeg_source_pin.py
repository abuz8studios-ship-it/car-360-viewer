from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts import build_ffmpeg_linux

GIT_ENV = {
    "GIT_AUTHOR_NAME": "LotVue Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "LotVue Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def _release_repo(tmp_path: Path) -> tuple[str, str, str]:
    """Upstream with an annotated release tag one commit behind the branch tip."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "--quiet")
    _git(upstream, "config", "uploadpack.allowAnySHA1InWant", "true")
    (upstream / "RELEASE").write_text("8.0\n")
    _git(upstream, "add", "RELEASE")
    _git(upstream, "commit", "--quiet", "-m", "release")
    released = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "tag", "-a", "n8.0", "-m", "FFmpeg 8.0")
    (upstream / "later.txt").write_text("after the release\n")
    _git(upstream, "add", "later.txt")
    _git(upstream, "commit", "--quiet", "-m", "later")
    return upstream.as_uri(), released, _git(upstream, "rev-parse", "HEAD")


@pytest.fixture
def isolated_git(monkeypatch):
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


def test_pinned_release_checkout_carries_its_tag(tmp_path, isolated_git) -> None:
    url, released, _later = _release_repo(tmp_path)
    destination = tmp_path / "src"
    destination.mkdir()

    head = build_ffmpeg_linux._clone_source(url, "n8.0", released, destination, tag="n8.0")

    assert head == released
    # FFmpeg's ffbuild/version.sh names the build with this command.
    assert _git(destination, "describe", "--tags", "--always") == "n8.0"


def test_release_tag_must_point_at_the_pinned_commit(tmp_path, isolated_git) -> None:
    url, _released, later = _release_repo(tmp_path)
    destination = tmp_path / "src"
    destination.mkdir()

    with pytest.raises(RuntimeError, match="release tag mismatch"):
        build_ffmpeg_linux._clone_source(url, "n8.0", later, destination, tag="n8.0")
