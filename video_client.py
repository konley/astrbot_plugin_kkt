"""Grok2API async video generation client (create / poll / download)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

logger = logging.getLogger("astrbot")

ProgressCallback = Callable[[int, str], Awaitable[None] | None]

_VALID_ASPECT = frozenset({"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"})
_VALID_RESOLUTION = frozenset({"480p", "720p", "1080p"})


class GrokVideoError(Exception):
    """User-facing or logged video API failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class VideoJobResult:
    request_id: str
    status: str
    progress: int
    video_url: str | None = None
    duration: int | None = None
    model: str | None = None
    error_code: str | None = None
    error_message: str | None = None


def normalize_api_base(base: str) -> str:
    """Strip trailing slash; ensure path ends with /v1 for OpenAI-style roots."""
    text = str(base or "").strip().rstrip("/")
    if not text:
        return ""
    lower = text.lower()
    if lower.endswith("/v1"):
        return text
    # bare host/port or custom prefix without /v1
    return f"{text}/v1"


def validate_duration(value: int) -> int:
    n = int(value)
    if n < 1 or n > 15:
        raise GrokVideoError("duration 必须在 1 到 15 秒之间", code="invalid_parameter")
    return n


def validate_aspect_ratio(value: str) -> str:
    text = str(value or "").strip() or "16:9"
    if text not in _VALID_ASPECT:
        raise GrokVideoError(
            "aspect_ratio 必须是 1:1、16:9、9:16、4:3、3:4、3:2 或 2:3",
            code="invalid_parameter",
        )
    return text


def validate_resolution(value: str) -> str:
    text = str(value or "").strip().lower() or "720p"
    if text not in _VALID_RESOLUTION:
        raise GrokVideoError(
            "resolution 必须是 480p、720p 或 1080p",
            code="invalid_parameter",
        )
    return text


def _is_loopback_or_unusable_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if not h:
        return True
    if h in {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}:
        return True
    if h.startswith("127."):
        return True
    return False


def resolve_media_url(api_base: str, url: str) -> str:
    """Resolve media URL against configured api_base.

    Upstream often returns ``http://127.0.0.1:8000/v1/videos/.../content`` because
    publicAPIBase is unset on their side. Rewrite loopback/relative paths to our
    configured origin so download hits the real gateway.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    if raw.startswith("data:") or raw.startswith("blob:"):
        return raw
    base = normalize_api_base(api_base)
    base_origin = ""
    if base:
        bp = urlparse(base)
        base_origin = f"{bp.scheme}://{bp.netloc}"

    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        path = parsed.path or ""
        # Gateway content/media paths that point at loopback must be rewritten.
        is_g2a_media = path.startswith("/v1/videos/") or path.startswith("/v1/media/")
        if is_g2a_media and base_origin and _is_loopback_or_unusable_host(parsed.hostname or ""):
            rewritten = base_origin + path
            if parsed.query:
                rewritten += "?" + parsed.query
            logger.info(
                "[kkt] video url rewrite loopback: %s -> %s",
                raw.split("?")[0][:120],
                rewritten.split("?")[0][:120],
            )
            return rewritten
        return raw
    if not base:
        return raw
    if raw.startswith("/"):
        return base_origin + raw if base_origin else raw
    return urljoin(base.rstrip("/") + "/", raw)


def content_url_for_job(api_base: str, request_id: str) -> str:
    """Build authenticated content URL on configured api_base."""
    base = normalize_api_base(api_base)
    rid = str(request_id or "").strip()
    if not base or not rid:
        return ""
    return f"{base.rstrip('/')}/videos/{rid}/content"


def _auth_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


def _read_error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
        msg = payload.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return fallback


def _is_retryable_status(status: int) -> bool:
    return status in {401, 403, 429} or status >= 500


async def _read_json(response: aiohttp.ClientResponse) -> Any:
    text = await response.text()
    if not text.strip():
        return None
    try:
        return await response.json(content_type=None)
    except Exception:
        return None


class GrokVideoClient:
    """Thin async client for grok2api /v1/videos/*."""

    def __init__(
        self,
        api_base: str,
        *,
        poll_interval: float = 3.0,
        timeout: float = 600.0,
        request_timeout: float = 60.0,
    ):
        self.api_base = normalize_api_base(api_base)
        self.poll_interval = max(1.0, float(poll_interval))
        self.timeout = max(30.0, float(timeout))
        self.request_timeout = max(10.0, float(request_timeout))

    def _url(self, path: str) -> str:
        base = self.api_base.rstrip("/")
        if not path.startswith("/"):
            path = "/" + path
        # api_base already includes /v1
        if path.startswith("/v1/"):
            origin = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
            return origin + path
        return base + path

    async def create_video(
        self,
        api_key: str,
        *,
        model: str,
        prompt: str = "",
        duration: int = 8,
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
        image_url: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> str:
        duration = validate_duration(duration)
        aspect_ratio = validate_aspect_ratio(aspect_ratio)
        resolution = validate_resolution(resolution)
        model = str(model or "").strip()
        prompt = str(prompt or "").strip()
        image_url = str(image_url or "").strip() or None
        if not model:
            raise GrokVideoError("未配置 video_model", code="invalid_parameter")
        if not prompt and not image_url:
            raise GrokVideoError(
                "文本生视频必须提供提示词；图生视频可以省略提示词",
                code="invalid_request",
            )
        if not self.api_base:
            raise GrokVideoError("未配置 video_api_base", code="invalid_parameter")

        body: dict[str, Any] = {
            "model": model,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
        }
        if prompt:
            body["prompt"] = prompt
        if image_url:
            body["image"] = {"url": image_url}

        url = self._url("/videos/generations")
        logger.info(
            "[kkt] video create: url=%s model=%s duration=%s aspect=%s res=%s "
            "prompt_len=%d has_image=%s",
            url,
            model,
            duration,
            aspect_ratio,
            resolution,
            len(prompt),
            bool(image_url),
        )

        async def _do(sess: aiohttp.ClientSession) -> str:
            headers = _auth_headers(api_key)
            headers["Content-Type"] = "application/json"
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            async with sess.post(url, json=body, headers=headers, timeout=timeout) as resp:
                payload = await _read_json(resp)
                if resp.status >= 400:
                    msg = _read_error_message(
                        payload, f"创建视频任务失败 HTTP {resp.status}"
                    )
                    logger.error(
                        "[kkt] video create failed: status=%s msg=%s",
                        resp.status,
                        msg[:300],
                    )
                    raise GrokVideoError(
                        msg,
                        code="create_failed",
                        status=resp.status,
                        retryable=_is_retryable_status(resp.status),
                    )
                request_id = ""
                if isinstance(payload, dict):
                    rid = payload.get("request_id")
                    if isinstance(rid, str):
                        request_id = rid.strip()
                if not request_id:
                    logger.error("[kkt] video create: missing request_id payload=%s", payload)
                    raise GrokVideoError(
                        "上游未返回 request_id",
                        code="invalid_response",
                        status=resp.status,
                    )
                logger.info("[kkt] video create ok: request_id=%s", request_id)
                return request_id

        if session is not None:
            return await _do(session)
        async with aiohttp.ClientSession() as sess:
            return await _do(sess)

    async def get_video(
        self,
        api_key: str,
        request_id: str,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> VideoJobResult:
        rid = str(request_id or "").strip()
        if not rid:
            raise GrokVideoError("request_id 为空", code="invalid_parameter")
        url = self._url(f"/videos/{rid}")

        async def _do(sess: aiohttp.ClientSession) -> VideoJobResult:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            async with sess.get(
                url, headers=_auth_headers(api_key), timeout=timeout
            ) as resp:
                payload = await _read_json(resp)
                if resp.status >= 400:
                    msg = _read_error_message(
                        payload, f"查询视频任务失败 HTTP {resp.status}"
                    )
                    logger.warning(
                        "[kkt] video poll failed: request_id=%s status=%s msg=%s",
                        rid,
                        resp.status,
                        msg[:200],
                    )
                    raise GrokVideoError(
                        msg,
                        code="poll_failed",
                        status=resp.status,
                        retryable=_is_retryable_status(resp.status),
                    )
                return self._parse_status(rid, payload)

        if session is not None:
            return await _do(session)
        async with aiohttp.ClientSession() as sess:
            return await _do(sess)

    def _parse_status(self, request_id: str, payload: Any) -> VideoJobResult:
        if not isinstance(payload, dict):
            raise GrokVideoError("视频状态响应无效", code="invalid_response")
        status_raw = str(payload.get("status") or "").strip().lower()
        # map internal completed -> done if any gateway returns it
        if status_raw in {"completed", "complete", "success"}:
            status_raw = "done"
        if status_raw in {"queued", "in_progress", "processing", "running"}:
            status_raw = "pending"
        if status_raw not in {"pending", "done", "failed"}:
            raise GrokVideoError(
                f"未知视频状态: {status_raw or 'empty'}",
                code="invalid_response",
            )
        progress = payload.get("progress")
        if isinstance(progress, (int, float)):
            prog = max(0, min(100, int(progress)))
        else:
            prog = 100 if status_raw == "done" else 0
        model = payload.get("model") if isinstance(payload.get("model"), str) else None
        video_url = None
        duration = None
        video = payload.get("video")
        if isinstance(video, dict):
            raw_url = video.get("url")
            if isinstance(raw_url, str) and raw_url.strip():
                video_url = resolve_media_url(self.api_base, raw_url.strip())
            if isinstance(video.get("duration"), (int, float)):
                duration = int(video["duration"])
        err_code = None
        err_msg = None
        error = payload.get("error")
        if isinstance(error, dict):
            if isinstance(error.get("code"), str):
                err_code = error["code"]
            if isinstance(error.get("message"), str):
                err_msg = error["message"]
        return VideoJobResult(
            request_id=request_id,
            status=status_raw,
            progress=prog,
            video_url=video_url,
            duration=duration,
            model=model,
            error_code=err_code,
            error_message=err_msg,
        )

    async def wait_video(
        self,
        api_key: str,
        request_id: str,
        *,
        on_progress: ProgressCallback | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> VideoJobResult:
        started = asyncio.get_event_loop().time()
        last_progress = -1

        async def _loop(sess: aiohttp.ClientSession) -> VideoJobResult:
            nonlocal last_progress
            while True:
                elapsed = asyncio.get_event_loop().time() - started
                if elapsed > self.timeout:
                    logger.error(
                        "[kkt] video wait timeout: request_id=%s timeout=%ss",
                        request_id,
                        self.timeout,
                    )
                    raise GrokVideoError(
                        f"视频生成超时（{int(self.timeout)}秒），请稍后重试",
                        code="timeout",
                    )
                result = await self.get_video(api_key, request_id, session=sess)
                if result.status == "done":
                    logger.info(
                        "[kkt] video done: request_id=%s progress=%s url_set=%s elapsed=%.1fs",
                        request_id,
                        result.progress,
                        bool(result.video_url),
                        elapsed,
                    )
                    if on_progress:
                        maybe = on_progress(100, "done")
                        if asyncio.iscoroutine(maybe):
                            await maybe
                    return result
                if result.status == "failed":
                    msg = result.error_message or "视频生成失败"
                    logger.error(
                        "[kkt] video failed: request_id=%s code=%s msg=%s",
                        request_id,
                        result.error_code,
                        msg[:300],
                    )
                    raise GrokVideoError(
                        msg,
                        code=result.error_code or "generation_failed",
                    )
                # pending：回调只用于内部任务记录；用户侧不发送百分比消息。
                should_report = (
                    result.progress != last_progress
                    and (result.progress - last_progress >= 5 or last_progress < 0)
                )
                if should_report and on_progress:
                    last_progress = result.progress
                    logger.info(
                        "[kkt] video progress: request_id=%s progress=%s%%",
                        request_id,
                        result.progress,
                    )
                    maybe = on_progress(result.progress, "pending")
                    if asyncio.iscoroutine(maybe):
                        await maybe
                await asyncio.sleep(self.poll_interval)

        if session is not None:
            return await _loop(session)
        async with aiohttp.ClientSession() as sess:
            return await _loop(sess)

    async def download_video(
        self,
        api_key: str,
        url: str,
        *,
        session: aiohttp.ClientSession | None = None,
        max_bytes: int = 200 * 1024 * 1024,
    ) -> tuple[bytes, str]:
        """Download video bytes. Returns (content, content_type)."""
        resolved = resolve_media_url(self.api_base, url)
        if not resolved:
            raise GrokVideoError("视频 URL 为空", code="invalid_response")
        if resolved.startswith("data:"):
            raise GrokVideoError("不支持 data URL 视频下载", code="invalid_response")

        logger.info(
            "[kkt] video download start: url=%s",
            resolved.split("?")[0][:200],
        )

        async def _do(sess: aiohttp.ClientSession) -> tuple[bytes, str]:
            headers = _auth_headers(api_key)
            # content endpoint needs auth; public CDN may not
            timeout = aiohttp.ClientTimeout(total=max(60.0, self.request_timeout * 3))
            async with sess.get(resolved, headers=headers, timeout=timeout) as resp:
                if resp.status >= 400:
                    # retry once without auth for pure CDN urls
                    if resp.status in {401, 403} and not resolved.rstrip("/").endswith(
                        "/content"
                    ):
                        async with sess.get(resolved, timeout=timeout) as resp2:
                            if resp2.status >= 400:
                                msg = f"下载视频失败 HTTP {resp2.status}"
                                raise GrokVideoError(
                                    msg, code="download_failed", status=resp2.status
                                )
                            return await self._read_body(resp2, max_bytes)
                    msg = f"下载视频失败 HTTP {resp.status}"
                    logger.error("[kkt] video download failed: status=%s", resp.status)
                    raise GrokVideoError(
                        msg, code="download_failed", status=resp.status
                    )
                return await self._read_body(resp, max_bytes)

        if session is not None:
            return await _do(session)
        async with aiohttp.ClientSession() as sess:
            return await _do(sess)

    async def _read_body(
        self, resp: aiohttp.ClientResponse, max_bytes: int
    ) -> tuple[bytes, str]:
        ctype = (resp.headers.get("Content-Type") or "video/mp4").split(";")[0].strip()
        cl = resp.headers.get("Content-Length")
        if cl and cl.isdigit() and int(cl) > max_bytes:
            raise GrokVideoError(
                f"视频过大（>{max_bytes // (1024 * 1024)}MB）",
                code="media_too_large",
            )
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise GrokVideoError(
                    f"视频过大（>{max_bytes // (1024 * 1024)}MB）",
                    code="media_too_large",
                )
            chunks.append(chunk)
        data = b"".join(chunks)
        logger.info(
            "[kkt] video download ok: bytes=%d content_type=%s",
            len(data),
            ctype,
        )
        if not data:
            raise GrokVideoError("下载到空视频", code="invalid_response")
        return data, ctype

    async def generate(
        self,
        api_keys: list[str],
        *,
        model: str,
        prompt: str = "",
        duration: int = 8,
        aspect_ratio: str = "16:9",
        resolution: str = "720p",
        image_url: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[VideoJobResult, bytes, str]:
        """Create + wait + download. Key rotation only on create failures."""
        keys = [k.strip() for k in api_keys if str(k or "").strip()]
        if not keys:
            raise GrokVideoError("未配置 video_api_key", code="missing_key")

        last_err: GrokVideoError | None = None
        async with aiohttp.ClientSession() as session:
            request_id = ""
            used_key = keys[0]
            for idx, key in enumerate(keys):
                try:
                    request_id = await self.create_video(
                        key,
                        model=model,
                        prompt=prompt,
                        duration=duration,
                        aspect_ratio=aspect_ratio,
                        resolution=resolution,
                        image_url=image_url,
                        session=session,
                    )
                    used_key = key
                    break
                except GrokVideoError as exc:
                    last_err = exc
                    if exc.retryable and idx + 1 < len(keys):
                        logger.warning(
                            "[kkt] video create retry next key: idx=%d/%d status=%s",
                            idx + 1,
                            len(keys),
                            exc.status,
                        )
                        continue
                    raise
            if not request_id:
                raise last_err or GrokVideoError("创建视频任务失败", code="create_failed")

            result = await self.wait_video(
                used_key,
                request_id,
                session=session,
            )
            download_url = resolve_media_url(self.api_base, result.video_url or "")
            fallback = content_url_for_job(self.api_base, request_id)
            if not download_url:
                download_url = fallback
                logger.info(
                    "[kkt] video url missing, fallback content: %s", download_url
                )
            try:
                content, ctype = await self.download_video(
                    used_key, download_url, session=session
                )
            except GrokVideoError as exc:
                # 若上游给了坏 host 或鉴权路径不对，强制走配置基址 content
                if fallback and download_url != fallback and exc.code == "download_failed":
                    logger.warning(
                        "[kkt] video download retry via content endpoint: from=%s to=%s err=%s",
                        download_url.split("?")[0][:120],
                        fallback,
                        exc.message[:120],
                    )
                    content, ctype = await self.download_video(
                        used_key, fallback, session=session
                    )
                    download_url = fallback
                else:
                    raise
            result = VideoJobResult(
                request_id=result.request_id,
                status=result.status,
                progress=result.progress,
                video_url=download_url,
                duration=result.duration,
                model=result.model,
            )
            return result, content, ctype
