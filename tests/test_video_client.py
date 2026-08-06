"""Unit tests for grok2api video client helpers."""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ensure_aiohttp() -> None:
    if "aiohttp" in sys.modules:
        return
    aiohttp = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, *a, **k):
            pass

    class ClientSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    aiohttp.ClientTimeout = ClientTimeout
    aiohttp.ClientSession = ClientSession
    sys.modules["aiohttp"] = aiohttp


_ensure_aiohttp()

from video_client import (  # noqa: E402
    GrokVideoClient,
    GrokVideoError,
    normalize_api_base,
    resolve_media_url,
    validate_aspect_ratio,
    validate_duration,
    validate_resolution,
)


def test_normalize_api_base():
    assert normalize_api_base("http://x:62125") == "http://x:62125/v1"
    assert normalize_api_base("http://x:62125/") == "http://x:62125/v1"
    assert normalize_api_base("http://x:62125/v1") == "http://x:62125/v1"
    assert normalize_api_base("http://x:62125/v1/") == "http://x:62125/v1"
    assert normalize_api_base("") == ""


def test_resolve_media_url():
    base = "https://g2a.example/v1"
    assert resolve_media_url(base, "https://cdn.example/a.mp4") == "https://cdn.example/a.mp4"
    assert (
        resolve_media_url(base, "/v1/videos/video_1/content")
        == "https://g2a.example/v1/videos/video_1/content"
    )
    assert resolve_media_url(base, "data:image/png;base64,xx") == "data:image/png;base64,xx"
    # 上游误返回本机 127.0.0.1:8000 时必须改写到配置基址
    assert (
        resolve_media_url(base, "http://127.0.0.1:8000/v1/videos/video_1/content")
        == "https://g2a.example/v1/videos/video_1/content"
    )
    assert (
        resolve_media_url(base, "http://localhost:8000/v1/videos/video_x/content")
        == "https://g2a.example/v1/videos/video_x/content"
    )


def test_validate_params():
    assert validate_duration(8) == 8
    try:
        validate_duration(16)
        assert False
    except GrokVideoError as exc:
        assert "1 到 15" in str(exc)
    assert validate_aspect_ratio("9:16") == "9:16"
    assert validate_resolution("1080p") == "1080p"
    try:
        validate_resolution("2k")
        assert False
    except GrokVideoError:
        pass


def test_parse_status_pending_and_done():
    client = GrokVideoClient("http://localhost:1")
    pending = client._parse_status(
        "video_abc",
        {"status": "pending", "model": "Web/grok-imagine-video", "progress": 42},
    )
    assert pending.status == "pending"
    assert pending.progress == 42
    assert pending.video_url is None

    done = client._parse_status(
        "video_abc",
        {
            "status": "done",
            "progress": 100,
            "video": {"url": "/v1/videos/video_abc/content", "duration": 8},
        },
    )
    assert done.status == "done"
    assert done.progress == 100
    assert done.video_url.endswith("/v1/videos/video_abc/content")
    assert done.duration == 8

    failed = client._parse_status(
        "video_abc",
        {"status": "failed", "error": {"code": "x", "message": "boom"}},
    )
    assert failed.status == "failed"
    assert failed.error_message == "boom"
