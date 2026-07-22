"""AstrBot plugin for NewAPI image generation and editing."""

import asyncio
import base64
import json
import os
import re
import time
from datetime import date
from pathlib import Path
from random import choice
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


def build_help_text() -> str:
    """Build the help text for the fixed image commands."""
    return (
        "Hajimi 图片生成\n"
        "用法：/hajimi <提示词> 或 /kkt <提示词>\n"
        "回复图片后：/hajimi <编辑提示词> 或 /kkt <编辑提示词>\n"
        "帮助：/hajimi help 或 /kkt help\n"
        "支持文生图、回复图片编辑和 @用户头像参考图。"
    )


@register(
    "astrbot_plugin_kkt",
    "konley",
    "调用 NewAPI 生成或编辑图片",
    "0.3.3",
)
class KktImagePlugin(Star):
    """Generate or edit images through an OpenAI-compatible endpoint."""

    # 匹配指令名后的参数；支持 /kkt帮助、/kkt help、/kkt ?
    _CMD_ARG_RE = re.compile(
        r"^/?(?:hajimi|kkt)(?:帮助|help|\?)?(?:\s+|$)(.*)$",
        re.IGNORECASE | re.DOTALL,
    )
    # AstrBot 把 At 序列化成 @昵称 或 @昵称(QQ号) 时用于剔除
    _AT_TOKEN_RE = re.compile(
        r"@[\w\u4e00-\u9fff\-·.]+(?:\(\d+\))?",
        re.UNICODE,
    )

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        config = config or {}
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
        # 出图后引用触发指令的那条消息（默认开）
        self.reply_with_quote = bool(config.get("reply_with_quote", True))
        # 触发生图时对原消息做 QQ 表情回应（不是文字回复）
        self.reaction_emoji_enabled = bool(config.get("reaction_emoji_enabled", True))
        self.reaction_emoji_list = self._parse_emoji_ids(
            config.get("reaction_emoji_list", [147])
        )
        strategy = str(config.get("reaction_emoji_strategy", "随机")).strip()
        self.reaction_emoji_strategy = (
            strategy if strategy in {"随机", "顺序循环"} else "随机"
        )
        self.reaction_emoji_type = "1"
        # 防刷：每用户独立 CD（秒）；0=关闭；管理员不受限
        self.cooldown_seconds = max(0, int(config.get("cooldown_seconds", 15)))
        # 单日全服总调用次数上限；0=不限制；超出后仅管理员可继续
        self.daily_quota = max(0, int(config.get("daily_quota", 50)))
        self.cleanup_delay = max(5, int(config.get("cleanup_delay", 15)))
        self.temp_dir = Path(get_astrbot_data_path()) / "plugin_data" / "kkt"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.quota_path = self.temp_dir / "daily_quota.json"
        # 内存：sender_id -> 上次成功触发生图的 monotonic 时间
        self._user_last_call: dict[str, float] = {}
        self._quota_lock = asyncio.Lock()
        self._help_text = build_help_text()
        logger.info(
            "[kkt] 插件已加载: commands=/hajimi,/kkt blacklist_count=%d model=%s "
            "endpoint=%s reply_with_quote=%s reaction_enabled=%s reaction_count=%d "
            "cooldown=%ds daily_quota=%d enable_at_avatar=%s",
            len(self.group_blacklist),
            self.model,
            f"{self.api_base}/chat/completions",
            self.reply_with_quote,
            self.reaction_emoji_enabled,
            len(self.reaction_emoji_list),
            self.cooldown_seconds,
            self.daily_quota,
            self.enable_at_avatar,
        )
        self._cleanup_stale_files()

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

    @staticmethod
    def _coerce_positive_int(value) -> int | None:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @classmethod
    def _parse_emoji_ids(cls, value) -> list[int]:
        """解析 0~5 个 QQ emoji_id；空列表表示不回应。"""
        if value is None:
            return []
        if isinstance(value, str):
            value = value.replace("，", ",").split(",")
        if not isinstance(value, list):
            value = [value]
        result: list[int] = []
        for item in value:
            emoji_id = cls._coerce_positive_int(item)
            if emoji_id is not None and emoji_id not in result:
                result.append(emoji_id)
            if len(result) >= 5:
                break
        return result

    @staticmethod
    def _extract_reaction_message_id(event: AstrMessageEvent) -> int | None:
        """多源提取原消息 ID，对齐 link_resolver。"""
        raw = getattr(event.message_obj, "raw_message", None)
        candidates: list[object] = []
        if isinstance(raw, dict):
            candidates.append(raw.get("message_id"))
        elif raw is not None and hasattr(raw, "message_id"):
            candidates.append(getattr(raw, "message_id", None))
        candidates.append(getattr(event.message_obj, "message_id", None))
        for value in candidates:
            if value is None:
                continue
            try:
                mid = int(value)
            except (TypeError, ValueError):
                continue
            if mid > 0:
                return mid
        return None

    async def _send_reaction_emoji(self, event: AstrMessageEvent) -> None:
        """对触发指令的消息做 QQ 表情回应（不是发文字）。

        实现参考 astrbot_plugin_link_resolver：bot.set_msg_emoji_like。
        仅群聊 + aiocqhttp/NapCat；失败只记日志，不影响出图。
        """
        if not self.reaction_emoji_enabled:
            return
        if not self.reaction_emoji_list:
            logger.debug("[kkt] 表情回应跳过: 列表为空")
            return
        if not event.get_group_id():
            logger.debug("[kkt] 表情回应跳过: 非群消息")
            return
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "set_msg_emoji_like"):
            logger.debug("[kkt] 表情回应跳过: 平台不支持 set_msg_emoji_like")
            return
        message_id = self._extract_reaction_message_id(event)
        if message_id is None:
            logger.debug("[kkt] 表情回应跳过: 无法获取 message_id")
            return

        if self.reaction_emoji_strategy == "顺序循环":
            emoji_ids = list(self.reaction_emoji_list)
        else:
            emoji_ids = [choice(self.reaction_emoji_list)]

        for emoji_id in emoji_ids:
            try:
                await bot.set_msg_emoji_like(
                    message_id=message_id,
                    emoji_id=emoji_id,
                    emoji_type=self.reaction_emoji_type,
                    set=True,
                )
                logger.info(
                    "[kkt] 表情回应成功: message_id=%s emoji_id=%s",
                    message_id,
                    emoji_id,
                )
            except Exception as exc:
                logger.warning(
                    "[kkt] 表情回应失败: message_id=%s emoji_id=%s error=%s",
                    message_id,
                    emoji_id,
                    str(exc)[:200],
                )
            if len(emoji_ids) > 1:
                await asyncio.sleep(0.5)

    def _build_image_chain(self, event: AstrMessageEvent, image_path: str) -> list:
        """组装出图消息链；可选前置 Reply 引用触发指令的消息。"""
        chain: list = []
        if self.reply_with_quote:
            message_id = self._extract_reaction_message_id(event)
            if message_id is not None:
                chain.append(Comp.Reply(id=message_id))
            else:
                logger.debug("[kkt] 引用回复跳过: 无法获取 message_id")
        chain.append(Comp.Image(file=str(image_path)))
        return chain

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _check_user_cooldown(self, event: AstrMessageEvent) -> str | None:
        """检查 per-user CD；管理员跳过。返回提示文案或 None。"""
        if self.cooldown_seconds <= 0:
            return None
        if self._is_admin(event):
            return None
        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            return None
        last = self._user_last_call.get(sender_id)
        if last is None:
            return None
        elapsed = time.monotonic() - last
        remain = self.cooldown_seconds - elapsed
        if remain > 0:
            return f"操作太快了，请 {int(remain) + 1} 秒后再试。"
        return None

    def _mark_user_cooldown(self, event: AstrMessageEvent) -> None:
        if self.cooldown_seconds <= 0:
            return
        if self._is_admin(event):
            return
        sender_id = str(event.get_sender_id() or "").strip()
        if sender_id:
            self._user_last_call[sender_id] = time.monotonic()

    def _load_quota_state(self) -> dict:
        today = date.today().isoformat()
        default = {"date": today, "count": 0}
        try:
            if not self.quota_path.exists():
                return default
            data = json.loads(self.quota_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return default
            if str(data.get("date") or "") != today:
                return default
            count = int(data.get("count") or 0)
            return {"date": today, "count": max(0, count)}
        except Exception as exc:
            logger.warning("[kkt] 读取日配额失败: %s", exc)
            return default

    def _save_quota_state(self, state: dict) -> None:
        try:
            self.quota_path.parent.mkdir(parents=True, exist_ok=True)
            self.quota_path.write_text(
                json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("[kkt] 写入日配额失败: %s", exc)

    async def _check_and_consume_daily_quota(
        self, event: AstrMessageEvent
    ) -> str | None:
        """检查并消耗单日总配额。

        - daily_quota<=0：不限制
        - 未超限：count+1 后放行
        - 已超限：仅管理员可继续（管理员调用不计入额外占用，也可继续调用）
        """
        if self.daily_quota <= 0:
            return None

        async with self._quota_lock:
            state = self._load_quota_state()
            used = int(state.get("count") or 0)
            is_admin = self._is_admin(event)

            if used >= self.daily_quota:
                if is_admin:
                    logger.info(
                        "[kkt] 日配额已满但仍允许管理员: used=%d limit=%d",
                        used,
                        self.daily_quota,
                    )
                    return None
                return (
                    f"今日生图总配额已用完（{used}/{self.daily_quota}），"
                    "请明天再试，或联系管理员。"
                )

            state["count"] = used + 1
            state["date"] = date.today().isoformat()
            self._save_quota_state(state)
            logger.info(
                "[kkt] 日配额消耗: used=%d/%d admin=%s",
                state["count"],
                self.daily_quota,
                is_admin,
            )
            return None

    @classmethod
    def _command_arg_from_text(cls, text: str) -> str | None:
        """从完整指令文本中截取命令后的参数；匹配失败返回 None。"""
        text = (text or "").strip()
        if not text:
            return None
        match = cls._CMD_ARG_RE.match(text)
        if not match:
            return None
        return match.group(1).strip()

    @classmethod
    def _strip_at_tokens(cls, text: str) -> str:
        """去掉 @昵称 / @昵称(QQ) 噪声，保留真实提示词。"""
        cleaned = cls._AT_TOKEN_RE.sub(" ", text or "")
        return re.sub(r"\s+", " ", cleaned).strip()

    @classmethod
    def _is_help_token(cls, text: str) -> bool:
        return (text or "").strip().lower() in {"帮助", "help", "?"}

    @classmethod
    def _extract_prompt(cls, event: AstrMessageEvent, prompt: str) -> str:
        """从消息中恢复命令后的文字提示词。

        AstrBot 的 GreedyStr 会把 At 组件序列化成 ``@昵称(QQ)``，导致
        ``/kkt @user 换装`` 的 prompt 只剩 ``@昵称(QQ)``，后面正文丢失。
        优先只拼 Plain 段（At 不在 Plain 里），再回退到整句并剥离 @ token。
        """
        prompt = (prompt or "").strip()

        candidates: list[str] = []

        # 1) 仅 Plain 段（@ 是独立 At 组件，不会混进这里）
        plain_text = "".join(
            getattr(component, "text", "") or ""
            for component in event.get_messages()
            if isinstance(component, Comp.Plain)
        )
        from_plain = cls._command_arg_from_text(plain_text)
        if from_plain is not None:
            candidates.append(from_plain)

        # 2) 整句 message_str（可能含 @昵称(QQ)）
        raw = (event.get_message_str() or "").strip()
        from_raw = cls._command_arg_from_text(raw)
        if from_raw is not None:
            candidates.append(from_raw)

        # 3) 框架传入的 GreedyStr 参数
        candidates.append(prompt)

        for candidate in candidates:
            text = candidate.strip()
            if not text:
                continue
            if cls._is_help_token(text):
                return text
            cleaned = cls._strip_at_tokens(text) if "@" in text else text
            if cleaned:
                return cleaned

        # 全是空 / 纯 @：返回空，交给后续 help 或引用文案逻辑
        return ""

    @filter.command("hajimi", alias={"kkt"})
    async def handle_command(self, event: AstrMessageEvent, prompt: GreedyStr = ""):
        group_id = str(event.get_group_id() or "").strip()
        if group_id and group_id in self.group_blacklist:
            logger.debug("[kkt] 忽略黑名单群消息: group_id=%s", group_id)
            return

        prompt = self._extract_prompt(event, prompt)
        logger.info(
            "[kkt] 指令匹配: command=%s prompt=%r",
            event.get_message_str().split()[0] if event.get_message_str().split() else "",
            prompt[:200],
        )
        event.stop_event()

        # 先收集引用图文，再判断 help。
        # 否则裸 /kkt + 引用文案 会在读引用之前就被当成空 prompt 返回帮助。
        try:
            image_parts, quoted_prompt = await self._collect_images(event)
        except Exception as exc:
            logger.warning(f"[kkt] 读取引用内容失败: {exc}")
            image_parts, quoted_prompt = [], ""

        if quoted_prompt:
            prompt = f"{quoted_prompt}\n{prompt}".strip() if prompt else quoted_prompt
        elif self._is_help_token(prompt):
            prompt = ""

        logger.info(
            "[kkt] 输入解析完成: prompt_length=%d image_count=%d quoted_prompt=%s",
            len(prompt),
            len(image_parts),
            bool(quoted_prompt),
        )
        if not prompt and not image_parts:
            yield event.plain_result(self._help_text)
            return

        if not self.api_key:
            logger.error("[kkt] 未配置 API Key")
            yield event.plain_result(
                "未配置 NewAPI Key，请在 AstrBot WebUI 的 Hajimi 图片生成插件配置中填写 api_key。"
            )
            return

        # per-user CD（管理员跳过）
        cd_msg = self._check_user_cooldown(event)
        if cd_msg:
            yield event.plain_result(cd_msg)
            return

        # 单日总配额（超限后仅管理员可继续）
        quota_msg = await self._check_and_consume_daily_quota(event)
        if quota_msg:
            yield event.plain_result(quota_msg)
            return

        # 通过限流后再记 CD，避免配额/校验失败也吃 CD
        self._mark_user_cooldown(event)

        # 开始干活前先表情回应原消息（不阻塞生图）
        asyncio.create_task(self._send_reaction_emoji(event))

        try:
            logger.info("[kkt] 开始调用图像 API: model=%s image_count=%d", self.model, len(image_parts))
            result = await self._request_image(prompt, image_parts)
            if not result:
                logger.error("[kkt] API 调用完成但未解析出图片")
                yield event.plain_result("API 返回中没有找到图片，请检查模型和接口响应格式。")
                return
            image_path = await self._materialize_image(result)
            if not image_path:
                yield event.plain_result("图片下载或解析失败，请稍后重试。")
                return
            logger.info("[kkt] 图片处理成功: path=%s", image_path)
            yield event.chain_result(self._build_image_chain(event, image_path))
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

        # 无现成图片时：enable_at_avatar 开启则收集全部 @ 用户头像（支持 N 个）
        if self.enable_at_avatar and not images:
            seen_qq: set[str] = set()
            for component in event.get_messages():
                if not isinstance(component, Comp.At):
                    continue
                qq = getattr(component, "qq", None) or getattr(component, "target", None)
                if qq is None or str(qq) in {"", "0", "all"}:
                    continue
                qq_str = str(qq).strip()
                if not qq_str or qq_str in seen_qq:
                    continue
                # 跳过 @机器人自己（若能取到）
                try:
                    self_id = str(event.get_self_id() or "").strip()
                except Exception:
                    self_id = ""
                if self_id and qq_str == self_id:
                    continue
                seen_qq.add(qq_str)
                avatar = Comp.Image.fromURL(
                    f"https://q1.qlogo.cn/g?b=qq&nk={qq_str}&s=640"
                )
                await add_image(avatar)
            if seen_qq:
                logger.info("[kkt] 收集 @ 头像: count=%d qqs=%s", len(seen_qq), list(seen_qq)[:10])
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
        logger.debug(
            "[kkt] 请求准备完成: endpoint=%s model=%s prompt_length=%d image_count=%d payload_bytes≈%d",
            endpoint,
            self.model,
            len(prompt),
            len(image_parts),
            len(json.dumps(payload, ensure_ascii=False)),
        )

        for attempt in range(self.max_retry + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    logger.info("[kkt] API 请求发送: attempt=%d/%d", attempt + 1, self.max_retry + 1)
                    async with session.post(endpoint, headers=headers, json=payload) as response:
                        raw = await response.text()
                        logger.info(
                            "[kkt] API 响应: attempt=%d status=%d bytes=%d",
                            attempt + 1,
                            response.status,
                            len(raw),
                        )
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
                            logger.info(
                                "[kkt] API 图片解析成功: source=%s",
                                "data_url" if image.startswith("data:") else "url",
                            )
                            return image
                        logger.error(
                            "[kkt] API JSON 未找到图片: top_keys=%s choices=%d",
                            list(data)[:20] if isinstance(data, dict) else type(data).__name__,
                            len(data.get("choices", [])) if isinstance(data, dict) else 0,
                        )
                        raise RuntimeError("API 响应中未找到图片")
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                last_error = str(exc)
                logger.error(
                    "[kkt] API 请求失败: attempt=%d error=%s",
                    attempt + 1,
                    last_error[:300],
                )
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
