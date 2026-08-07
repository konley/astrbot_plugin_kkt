"""AstrBot WebUI APIs for the kkt control panel."""

from __future__ import annotations

import re
import time
from datetime import date
from typing import Any, ClassVar
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from astrbot.api import logger

try:
    from quart import jsonify, request
except ImportError:  # pragma: no cover - AstrBot runtime provides Quart
    jsonify = None
    request = None


class KktWebApiMixin:
    """Bound WebUI API handlers; the Star class supplies runtime state."""

    _WEBUI_PREFIX = "[kkt][webui]"
    _WEB_GROUPS: ClassVar[set[str]] = {
        "command_settings",
        "group_settings",
        "main_image_settings",
        "image2_settings",
        "grok_image_settings",
        "video_settings",
        "generation_settings",
        "input_behavior_settings",
        "reaction_settings",
        "quota_settings",
        "media_settings",
        "moderation_settings",
    }
    _WEB_FIELDS: ClassVar[dict[str, tuple[str, type]]] = {
        # Command aliases
        "main_command_aliases": ("command_settings", list),
        "image2_command_aliases": ("command_settings", list),
        "grok_command_aliases": ("command_settings", list),
        "grok2_command_aliases": ("command_settings", list),
        "video_command_aliases": ("command_settings", list),
        "main_gif_aliases": ("command_settings", list),
        "main_gif2_aliases": ("command_settings", list),
        "image2_gif_aliases": ("command_settings", list),
        "image2_gif2_aliases": ("command_settings", list),
        "kkgifzip_aliases": ("command_settings", list),
        "grokpack_aliases": ("command_settings", list),
        "hajimipack_aliases": ("command_settings", list),
        "image2pack_aliases": ("command_settings", list),
        "grokvg_aliases": ("command_settings", list),
        # Channel credentials and models
        "group_blacklist": ("group_settings", list),
        "api_base": ("main_image_settings", str),
        "api_key": ("main_image_settings", str),
        "backup_api_keys": ("main_image_settings", list),
        "model": ("main_image_settings", str),
        "main_route": ("main_image_settings", str),
        "image2_api_base": ("image2_settings", str),
        "image2_api_key": ("image2_settings", str),
        "image2_backup_api_keys": ("image2_settings", list),
        "image2_model": ("image2_settings", str),
        "image2_size": ("image2_settings", str),
        "image2_api_mode": ("image2_settings", str),
        "grok_api_base": ("grok_image_settings", str),
        "grok_api_key": ("grok_image_settings", str),
        "grok_backup_api_keys": ("grok_image_settings", list),
        "video_api_base": ("video_settings", str),
        "video_api_key": ("video_settings", str),
        "video_backup_api_keys": ("video_settings", list),
        "video_model": ("video_settings", str),
        "video_duration": ("video_settings", int),
        "video_aspect_ratio": ("video_settings", str),
        "video_resolution": ("video_settings", str),
        "video_poll_interval": ("video_settings", int),
        "video_timeout": ("video_settings", int),
        "video_max_concurrent": ("video_settings", int),
        "video_max_concurrent_per_user": ("video_settings", int),
        "video_cooldown_seconds": ("video_settings", int),
        "video_cleanup_delay": ("video_settings", int),
        "video_prompt_enhance": ("video_settings", bool),
        "video_style_prompt": ("video_settings", str),
        # Prompt and message behavior
        "animated_reference_frame": ("input_behavior_settings", str),
        "enable_reply_image": ("input_behavior_settings", bool),
        "enable_at_avatar": ("input_behavior_settings", bool),
        "label_images": ("input_behavior_settings", bool),
        "prefer_chinese_text": ("input_behavior_settings", bool),
        "prefer_cn_locale": ("input_behavior_settings", bool),
        "style_prompt": ("input_behavior_settings", str),
        "reply_with_quote": ("input_behavior_settings", bool),
        "reaction_emoji_enabled": ("reaction_settings", bool),
        "reaction_emoji_list": ("reaction_settings", list),
        "reaction_emoji_strategy": ("reaction_settings", str),
        # Request and retry
        "temperature": ("generation_settings", float),
        "timeout": ("generation_settings", int),
        "max_retry": ("generation_settings", int),
        "retry_delay": ("generation_settings", int),
        # Quota and estimated cost
        "cooldown_seconds": ("quota_settings", int),
        "daily_quota": ("quota_settings", int),
        "daily_quota_main": ("quota_settings", int),
        "daily_quota_image2": ("quota_settings", int),
        "daily_quota_video": ("quota_settings", int),
        "cost_main_usd": ("quota_settings", float),
        "cost_image2_usd": ("quota_settings", float),
        "cost_video_usd": ("quota_settings", float),
        # Media and moderation
        "cleanup_delay": ("media_settings", int),
        "gif_frame_size": ("media_settings", int),
        "gif_fps": ("media_settings", int),
        "gif_max_bytes": ("media_settings", int),
        "video_gif_max_duration": ("media_settings", int),
        "video_gif_max_dimension": ("media_settings", int),
        "video_gif_fps": ("media_settings", int),
        "video_gif_max_bytes": ("media_settings", int),
        "sensitive_filter_enabled": ("moderation_settings", bool),
        "sensitive_lexicon_path": ("moderation_settings", str),
        "sensitive_categories": ("moderation_settings", list),
    }
    _SECRET_FIELDS: ClassVar[set[str]] = {
        "api_key",
        "backup_api_keys",
        "image2_api_key",
        "image2_backup_api_keys",
        "grok_api_key",
        "grok_backup_api_keys",
        "video_api_key",
        "video_backup_api_keys",
    }

    def _register_webui_apis(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            return
        self._register_webui_api("dashboard", self._web_dashboard, ["GET"], "康康图控制台数据")
        self._register_webui_api("config", self._web_config, ["GET", "POST"], "读取或保存康康图控制台配置")
        self._register_webui_api("test-connection", self._web_test_connection, ["POST"], "测试康康图 API 连接")

    def _register_webui_api(self, route: str, handler, methods: list[str], desc: str) -> None:
        route_path = f"/astrbot_plugin_kkt/{route.strip('/')}"

        async def logged_handler(*args, **kwargs):
            started = time.monotonic()
            try:
                result = await handler(*args, **kwargs)
                logger.info(
                    "%s %s %s ok duration_ms=%d",
                    self._WEBUI_PREFIX,
                    ",".join(methods),
                    route,
                    int((time.monotonic() - started) * 1000),
                )
                return result
            except Exception:
                logger.exception("%s %s failed", self._WEBUI_PREFIX, route)
                raise

        logged_handler.__name__ = f"webui_{handler.__name__}"
        self.context.register_web_api(route_path, logged_handler, methods, desc)

    def _web_raw_config(self) -> dict[str, Any]:
        raw = getattr(self, "config", {})
        if isinstance(raw, dict):
            return raw
        return {}

    def _web_flat_config(self) -> dict[str, Any]:
        return self._flatten_plugin_config(self._web_raw_config())

    def _web_masked_config(self) -> dict[str, Any]:
        config = self._web_flat_config()
        result: dict[str, Any] = {}
        for key, value in config.items():
            if key in self._SECRET_FIELDS:
                if key.endswith("_backup_api_keys"):
                    result[f"{key}_count"] = (
                        len(value) if isinstance(value, list) else 0
                    )
                else:
                    result[f"{key}_configured"] = bool(str(value or "").strip())
                continue
            result[key] = value

        # The effective fallback is useful in the UI without exposing secrets.
        result["grok_api_base_effective"] = str(getattr(self, "grok_api_base", ""))
        result["video_api_base_effective"] = str(getattr(self, "video_api_base", ""))
        result["grok_api_key_effective_configured"] = bool(
            str(getattr(self, "grok_api_key", "") or "").strip()
        )
        result["video_api_key_effective_configured"] = bool(
            str(getattr(self, "video_api_key", "") or "").strip()
        )
        result["video_reuses_grok"] = not any(
            str(config.get(field) or "").strip()
            for field in ("video_api_base", "video_api_key")
        )
        # Alias fields are returned as effective values so old flat configs also
        # show the built-in defaults in the dashboard.
        for command_key, field in getattr(self, "_COMMAND_ALIAS_FIELDS", {}).items():
            result[field] = list(getattr(self, "_command_aliases", {}).get(command_key, []))
        return result

    async def _web_dashboard(self):
        usage = self._load_usage_state()
        channels = {}
        for channel in self._CHANNELS:
            bucket = usage["channels"].get(channel, {})
            channels[channel] = {
                "daily": int(bucket.get("daily") or 0),
                "total": int(bucket.get("total") or 0),
                "limit": int(self.channel_limits.get(channel, 0)),
                "cost": float(self._cost_usd_for_channel(channel)),
            }
        return jsonify(
            {
                "ok": True,
                "version": "0.17.0",
                "date": date.today().isoformat(),
                "channels": channels,
                "video": {
                    "inflight": int(getattr(self, "_video_global_inflight", 0)),
                    "limit": int(getattr(self, "video_max_concurrent", 0)),
                    "per_user_limit": int(getattr(self, "video_max_concurrent_per_user", 0)),
                },
                "config": self._web_masked_config(),
                "commands": self._command_catalog(),
                "tasks": self._get_task_logs(60),
            }
        )

    async def _web_config(self):
        if request.method == "GET":
            return jsonify({"ok": True, "config": self._web_masked_config()})
        body = await request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "请求格式无效"}), 400
        updates = body.get("updates", body)
        if not isinstance(updates, dict):
            return jsonify({"ok": False, "error": "updates 必须是对象"}), 400

        raw = self._web_raw_config()
        changed: list[str] = []
        for key, value in updates.items():
            if key not in self._WEB_FIELDS:
                continue
            if key in self._SECRET_FIELDS and not str(value or "").strip():
                continue
            group, converter = self._WEB_FIELDS[key]
            if isinstance(raw.get(group), dict):
                target = raw[group]
            else:
                target = raw
            try:
                if converter is bool:
                    if isinstance(value, str):
                        normalized = value.strip().casefold()
                        if normalized in {"true", "1", "yes", "on", "开", "开启"}:
                            converted = True
                        elif normalized in {"false", "0", "no", "off", "关", "关闭"}:
                            converted = False
                        else:
                            raise ValueError
                    else:
                        converted = bool(value)
                elif converter is int:
                    converted = int(value)
                elif converter is float:
                    converted = float(value)
                elif converter is list:
                    if value is None or value == "":
                        converted = []
                    elif isinstance(value, list):
                        converted = [str(item).strip() for item in value if str(item).strip()]
                    else:
                        text = str(value).replace("，", ",")
                        converted = [
                            item.strip()
                            for item in re.split(r"[\n,;|]+", text)
                            if item.strip()
                        ]
                else:
                    converted = str(value or "").strip()
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": f"配置项 {key} 格式无效"}), 400
            target[key] = converted
            changed.append(key)

        if not changed:
            return jsonify({"ok": False, "error": "没有可保存的配置项"}), 400
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            saver()
        logger.info("%s config saved fields=%s", self._WEBUI_PREFIX, changed)
        return jsonify(
            {
                "ok": True,
                "changed": changed,
                "reload_required": True,
                "message": "配置已保存，请在 AstrBot 插件管理中重载康康图。",
            }
        )

    @staticmethod
    def _api_origin(base: str) -> str:
        parsed = urlsplit(str(base or "").strip().rstrip("/"))
        path = parsed.path.rstrip("/")
        if path.lower().endswith("/v1"):
            path = path[:-3]
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")

    async def _web_test_connection(self):
        body = await request.get_json(silent=True)
        channel = str((body or {}).get("channel", "video")).strip().lower()
        if channel == "grok":
            base, key = self.grok_api_base, self.grok_api_key
        elif channel == "image2":
            base, key = self.image2_api_base, self.image2_api_key
        elif channel == "main":
            base, key = self.api_base, self.api_key
        elif channel in {"video", "grokvideo", "grokv"}:
            channel = "video"
            base, key = self.video_api_base, self.video_api_key
        else:
            return jsonify({"ok": False, "error": "未知通道"}), 400
        if not base:
            return jsonify({"ok": False, "error": "该通道未配置 API 地址"}), 400

        origin = self._api_origin(base)
        started = time.monotonic()
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            headers = {"Accept": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{origin}/healthz", headers=headers) as health:
                    health_status = health.status
                async with session.get(
                    f"{str(base).rstrip('/')}/models", headers=headers
                ) as models:
                    model_status = models.status
                    payload = await models.json(content_type=None)
            model_ids = []
            if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                model_ids = [
                    str(item.get("id"))
                    for item in payload["data"]
                    if isinstance(item, dict) and item.get("id")
                ]
            return jsonify(
                {
                    "ok": health_status < 400 and model_status < 400,
                    "channel": channel,
                    "health_status": health_status,
                    "models_status": model_status,
                    "models": model_ids[:40],
                    "latency_ms": int((time.monotonic() - started) * 1000),
                }
            )
        except Exception:
            logger.exception("%s connection test failed channel=%s", self._WEBUI_PREFIX, channel)
            return jsonify({"ok": False, "channel": channel, "error": "连接测试失败，请查看插件日志"}), 502
