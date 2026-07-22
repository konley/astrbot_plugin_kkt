"""AstrBot plugin for NewAPI image generation and editing."""

import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


def build_help_text(command: str, aliases: list[str], allow_without_slash: bool) -> str:
    """Build the command help text from the active configuration."""
    prefix = f"/{command}" if not allow_without_slash else f"/{command} 或 {command}"
    alias_text = "、".join(f"/{alias}" for alias in aliases) if aliases else "无"
    return (
        "Hajimi 图片生成\n"
        f"用法：{prefix} <提示词>\n"
        f"回复图片后：{prefix} <编辑提示词>\n"
        f"帮助：{prefix}帮助\n"
        f"当前唤醒词：{command}\n"
        f"可用别名：{alias_text}\n"
        "支持文生图、回复图片编辑和 @用户头像参考图。"
    )


@register(
    "astrbot_plugin_kkt",
    "konley",
    "调用 NewAPI 生成或编辑图片",
    "0.1.0",
)
class KktImagePlugin(Star):
    """Generate or edit images through an OpenAI-compatible endpoint."""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        config = config or {}
        configured_command = str(config.get("command", "hajimi")).strip().lower()
        self.command = configured_command if re.fullmatch(r"[a-z0-9_\-]+", configured_command) else "hajimi"
        self.aliases = [alias for alias in self._parse_words(config.get("aliases", [])) if alias != self.command]
        self.allow_without_slash = bool(config.get("allow_without_slash", False))
        self.group_blacklist = self._parse_group_ids(config.get("group_blacklist", []))
        self.api_base = str(
            config.get("api_base", "https://newapi.qianqianye.com/v1")
        ).rstrip("/")
        self.api_key = str(config.get("api_key", "")).strip() or os.getenv(
            "NEW_API_KEY", ""
        ).strip()
        self.model = str(config.get("model", "gemini-3.1-flash-image")).strip()
        self.temperature = max(0.0, min(2.0, float(config.get("temperature", 0.7))))
        self.timeout = max(10, int(config.get("timeout", 180)))
        self.max_retry = max(0, min(5, int(config.get("max_retry", 2))))
        self.retry_delay = max(0, int(config.get("retry_delay", 2)))
        self.enable_reply_image = bool(config.get("enable_reply_image", True))
        self.enable_at_avatar = bool(config.get("enable_at_avatar", False))
        self.cleanup_delay = max(5, int(config.get("cleanup_delay", 15)))
        self.temp_dir = Path(get_astrbot_data_path()) / "plugin_data" / "kkt"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._help_text = build_help_text(self.command, self.aliases, self.allow_without_slash)
        self._cleanup_stale_files()

    @staticmethod
    def _parse_words(value) -> list[str]:
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(
            word.strip().lower() for word in value
            if isinstance(word, str) and re.fullmatch(r"[a-z0-9_\-]+", word.strip(), re.IGNORECASE)
        ))

    @staticmethod
    def _parse_group_ids(value) -> set[str]:
        if isinstance(value, str):
            value = value.replace("，", ",").split(",")
        if not isinstance(value, list):
            return set()
        return {
            group_id.strip() for group_id in map(str, value)
            if re.fullmatch(r"\d+", group_id.strip())
        }

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_message(self, event: AstrMessageEvent):
        group_id = str(event.get_group_id() or "").strip()
        if group_id and group_id in self.group_blacklist:
            return

        raw = (event.message_str or "").strip()
        command_pattern = "|".join(re.escape(word) for word in [self.command, *self.aliases])
        prefix = r"/?" if self.allow_without_slash else r"/"
        command_match = re.match(
            rf"^{prefix}(?:{command_pattern})(?:帮助|help|\?)?(?:\s+([\s\S]*))?$",
            raw,
            re.IGNORECASE,
        )
        if not command_match:
            return

        event.stop_event()
        prompt = (command_match.group(1) or "").strip()
        if prompt.lower() in {"帮助", "help", "?"}:
            prompt = ""
        if not prompt:
            yield event.plain_result(self._help_text)
            return

        if not self.api_key:
            yield event.plain_result(
                "未配置 NewAPI Key，请在 AstrBot WebUI 的 Hajimi 图片生成插件配置中填写 api_key。"
            )
            return

        try:
            image_parts, quoted_prompt = await self._collect_images(event)
        except Exception as exc:
            logger.warning(f"[kkt] 读取引用内容失败: {exc}")
            image_parts, quoted_prompt = [], ""

        if quoted_prompt:
            prompt = f"{quoted_prompt}\n{prompt}".strip()

        if not prompt and not image_parts:
            yield event.plain_result(self._help_text)
            return

        try:
            result = await self._request_image(prompt, image_parts)
            if not result:
                yield event.plain_result("API 返回中没有找到图片，请检查模型和接口响应格式。")
                return
            image_path = await self._materialize_image(result)
            if not image_path:
                yield event.plain_result("图片下载或解析失败，请稍后重试。")
                return
            yield event.chain_result([Comp.Image(file=str(image_path))])
            self._schedule_cleanup(image_path)
        except Exception as exc:
            logger.error(f"[kkt] 图片生成失败: {exc}")
            yield event.plain_result(f"图片生成失败：{exc}")

    async def _collect_images(self, event: AstrMessageEvent) -> tuple[list[dict], str]:
        """Collect quoted text/images while ignoring automatic mentions."""
        images = []
        seen = set()
        quoted_texts = []

        async def add_image(component):
            if not isinstance(component, Comp.Image):
                return
            value = getattr(component, "url", None) or getattr(component, "file", None)
            if not value or value in seen:
                return
            seen.add(value)
            try:
                encoded = await component.convert_to_base64()
                images.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
            except Exception as exc:
                logger.warning(f"[kkt] 图片转 Base64 失败: {exc}")

        current_images = []
        quoted_images = []
        components = event.get_messages()
        for component in components:
            if isinstance(component, Comp.Image):
                value = getattr(component, "url", None) or getattr(component, "file", None)
                if value:
                    current_images.append(component)
            elif isinstance(component, Comp.Reply) and self.enable_reply_image:
                for quoted in component.chain or []:
                    if isinstance(quoted, Comp.Image):
                        quoted_images.append(quoted)
                    elif isinstance(quoted, Comp.Plain):
                        text = getattr(quoted, "text", "").strip()
                        if text:
                            quoted_texts.append(text)

        # Include both quoted and current images; quoted text and At components are ignored.
        for component in [*quoted_images, *current_images]:
            await add_image(component)

        if self.enable_at_avatar and not images:
            for component in event.get_messages():
                if isinstance(component, Comp.At):
                    qq = getattr(component, "qq", None) or getattr(component, "target", None)
                    if qq:
                        avatar = Comp.Image.fromURL(f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640")
                        await add_image(avatar)
                        break
        return images, "\n".join(quoted_texts)

    async def _request_image(self, prompt: str, image_parts: list[dict]) -> str | None:
        content = []
        if prompt:
            content.append({"type": "text", "text": prompt})
        content.extend(image_parts)
        if not content:
            content.append({"type": "text", "text": "请生成一张图片"})

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": self.temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        endpoint = f"{self.api_base}/chat/completions"
        last_error = "未知错误"

        for attempt in range(self.max_retry + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(endpoint, headers=headers, json=payload) as response:
                        raw = await response.text()
                        if response.status in {401, 403}:
                            raise RuntimeError("API Key 无效或没有模型权限")
                        if response.status == 429:
                            last_error = "API 请求频率或额度受限"
                            raise RuntimeError(last_error)
                        if response.status >= 500:
                            last_error = f"上游服务异常 HTTP {response.status}"
                            raise RuntimeError(last_error)
                        if response.status >= 400:
                            raise RuntimeError(f"API 请求失败 HTTP {response.status}: {raw[:200]}")
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            raise RuntimeError(f"API 返回非 JSON：{raw[:200]}") from exc
                        image = self._extract_image(data)
                        if image:
                            return image
                        raise RuntimeError("API 响应中未找到图片")
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                last_error = str(exc)
                if attempt < self.max_retry and not ("Key 无效" in last_error or "权限" in last_error):
                    await asyncio.sleep(self.retry_delay)
                    continue
                raise RuntimeError(last_error) from exc
        return None

    @staticmethod
    def _extract_image(data: dict) -> str | None:
        message = (data.get("choices") or [{}])[0].get("message") or {}
        for item in message.get("images") or []:
            value = item.get("url") if isinstance(item, dict) else None
            value = value or ((item.get("image_url") or {}).get("url") if isinstance(item, dict) else None)
            if value:
                return value
        content = message.get("content")
        parts = content if isinstance(content, list) else [content]
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "image_url":
                value = (part.get("image_url") or {}).get("url") or part.get("url")
                if value:
                    return value
            if isinstance(part, str):
                match = re.search(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", part)
                if match:
                    return match.group(0)
                match = re.search(r"https?://[^\s)>'\"]+", part)
                if match:
                    return match.group(0)
        return None

    async def _materialize_image(self, value: str) -> str | None:
        suffix = ".png"
        if value.startswith("data:image/"):
            header, encoded = value.split(",", 1)
            extension = header.split("/", 1)[1].split(";", 1)[0]
            suffix = ".jpg" if extension in {"jpeg", "jpg"} else f".{extension}"
            content = base64.b64decode(encoded)
        elif value.startswith("http://") or value.startswith("https://"):
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                async with session.get(value) as response:
                    if response.status != 200:
                        return None
                    content = await response.read()
            extension = Path(urlparse(value).path).suffix.lower()
            if extension in {".jpg", ".jpeg", ".webp", ".gif"}:
                suffix = extension
        else:
            return None

        path = self.temp_dir / f"kkt_{int(time.time() * 1000)}{suffix}"
        path.write_bytes(content)
        return str(path)

    def _schedule_cleanup(self, path: str):
        async def cleanup():
            await asyncio.sleep(self.cleanup_delay)
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

        asyncio.create_task(cleanup())

    def _cleanup_stale_files(self):
        cutoff = time.time() - 3600
        for path in self.temp_dir.glob("kkt_*"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    async def terminate(self):
        pass
