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
        "康康图\n"
        "用法：/kkt|/hajimi|/image2 <提示词>\n"
        "回复图可编辑；image2 仅 1 张参考图\n"
        "额度：/kkt额度；审核开关：/kkt审核"
    )


@register(
    "astrbot_plugin_kkt",
    "konley",
    "调用 NewAPI 生成或编辑图片",
    "0.6.5",
)
class KktImagePlugin(Star):
    """Generate or edit images through an OpenAI-compatible endpoint."""

    # Sensitive-lexicon Vocabulary 文件名 → WebUI 可选类别名
    _LEXICON_FILE_TO_CATEGORY: dict[str, str] = {
        "政治类型.txt": "政治",
        "反动词库.txt": "反动",
        "贪腐词库.txt": "贪腐",
        "暴恐词库.txt": "暴恐",
        "涉枪涉爆.txt": "涉枪涉爆",
        "色情类型.txt": "色情",
        "色情词库.txt": "色情",
        "广告类型.txt": "广告",
        "民生词库.txt": "民生",
        "COVID-19词库.txt": "COVID-19",
        "GFW补充词库.txt": "GFW",
        "非法网址.txt": "非法网址",
        "补充词库.txt": "补充",
        "其他词库.txt": "其他",
        "新思想启蒙.txt": "新思想",
        "网易前端过滤敏感词库.txt": "网易综合",
        "零时-Tencent.txt": "腾讯综合",
    }
    _SENSITIVE_REJECT_USER_MSG = "内容审核未通过，请修改提示词后重试。"

    # 匹配指令名后的参数；支持 /kkt帮助、/image2 help 等
    _CMD_ARG_RE = re.compile(
        r"^/?(?:hajimi|kkt|image2)(?:帮助|help|\?)?(?:\s+|$)(.*)$",
        re.IGNORECASE | re.DOTALL,
    )
    # AstrBot 把 At 序列化成 @昵称 或 @昵称(QQ号) 时用于剔除
    _AT_TOKEN_RE = re.compile(
        r"@[\w\u4e00-\u9fff\-·.]+(?:\(\d+\))?",
        re.UNICODE,
    )
    # 用户口头编号：图片1 / 图2 / image 3
    _USER_IMAGE_REF_RE = re.compile(
        r"(?:图片|圖|图|image)\s*([0-9]+)",
        re.IGNORECASE,
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
        # 主 Key 失败后按顺序尝试的备用 Key（同 api_base / model）
        self.backup_api_keys = self._parse_secret_list(
            config.get("backup_api_keys", [])
        )
        self.model = str(config.get("model", "gemini-3.1-flash-image")).strip()
        # /image2 独立通道：模型 + Key（Key 必填独立；未配置则 image2 不可用）
        self.image2_model = str(
            config.get("image2_model", "gpt-image-2")
        ).strip() or "gpt-image-2"
        self.image2_api_key = str(config.get("image2_api_key", "") or "").strip()
        self.image2_backup_api_keys = self._parse_secret_list(
            config.get("image2_backup_api_keys", [])
        )
        # 可选：image2 用不同基址，空则复用 api_base
        image2_base = str(config.get("image2_api_base", "") or "").strip().rstrip("/")
        self.image2_api_base = image2_base or self.api_base
        # image2 协议：images=走 /images/*；chat=走 chat/completions；auto=按模型猜测
        mode = str(config.get("image2_api_mode", "images") or "images").strip().lower()
        self.image2_api_mode = mode if mode in {"images", "chat", "auto"} else "images"
        self.image2_size = str(config.get("image2_size", "1024x1024") or "1024x1024").strip()
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
        # 多图时用【图N】标签交错说明，帮助模型对齐角色/物体
        self.label_images = bool(config.get("label_images", True))
        # 默认中文：气泡/标题/旁白等图内文字优先中文
        self.prefer_chinese_text = bool(config.get("prefer_chinese_text", True))
        # 轻量本地化：人物默认东亚/华人，画风与生活细节略偏国内习惯（不锁死场景/构图）
        self.prefer_cn_locale = bool(config.get("prefer_cn_locale", True))
        style_parts: list[str] = []
        if self.prefer_chinese_text:
            style_parts.append(
                "【画面文字语言】图中所有可读文字默认使用简体中文，包括但不限于："
                "对话气泡、旁白、标题、字幕、招牌、UI 文案、音效拟声（如「咔咔」「叮」）。"
                "仅当用户明确要求英文/其他语言，或品牌名、游戏名、专有名词本身必须保留原文时，才使用非中文。"
                "不要无故把中文提示画成全英文漫画分镜文案。"
            )
        if self.prefer_cn_locale:
            style_parts.append(
                "【人物与习惯·轻量默认，勿过度限制】"
                "1) 人物：用户未指定种族/国籍/外貌时，默认东亚华人常见外貌特征；"
                "若有参考图或@头像，优先还原参考人物，不要擅自换成外国人脸；"
                "用户明确要求其他外貌/种族/角色设定时，完全以用户为准。"
                "2) 画风：在不违背用户画风要求的前提下，可略偏国内常见二次元/国漫的清爽表现，"
                "不要强制单一画风或固定脸模。"
                "3) 生活细节：若出现当代日常物件，可自然使用中国常见物品，避免堆砌刻板符号；"
                "不要强行改写奇幻/异世界/明确海外等场景。"
            )
        custom_style = str(config.get("style_prompt", "") or "").strip()
        if custom_style:
            style_parts.append(custom_style)
        self.style_prompt = "\n".join(style_parts).strip()
        # 防刷：每用户独立 CD（秒）；0=关闭；管理员不受限
        self.cooldown_seconds = max(0, int(config.get("cooldown_seconds", 15)))
        # 单日全服总调用次数上限；0=不限制；超出后仅管理员可继续
        # 配置默认值；运行时可由管理员指令覆盖并写入 runtime 覆盖文件
        self._daily_quota_config_default = max(0, int(config.get("daily_quota", 50)))
        self.cleanup_delay = max(5, int(config.get("cleanup_delay", 15)))
        self.temp_dir = Path(get_astrbot_data_path()) / "plugin_data" / "kkt"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.quota_path = self.temp_dir / "daily_quota.json"
        self.quota_limit_override_path = self.temp_dir / "daily_quota_limit.json"
        # 本地 Sensitive-lexicon 前置审核（默认关；词库不随插件分发）
        # WebUI 为默认值；管理员指令可 runtime 覆盖并持久化
        self._sensitive_filter_config_default = bool(
            config.get("sensitive_filter_enabled", False)
        )
        self.sensitive_filter_override_path = (
            self.temp_dir / "sensitive_filter_enabled.json"
        )
        lexicon_path = str(config.get("sensitive_lexicon_path", "") or "").strip()
        self.sensitive_lexicon_path = (
            Path(lexicon_path).expanduser()
            if lexicon_path
            else (self.temp_dir / "Sensitive-lexicon")
        )
        self.sensitive_categories = self._parse_category_list(
            config.get("sensitive_categories", [])
        )
        # 内存：sender_id -> 上次成功触发生图的 monotonic 时间
        self._user_last_call: dict[str, float] = {}
        self._quota_lock = asyncio.Lock()
        self.daily_quota = self._load_daily_quota_limit()
        self._help_text = build_help_text()
        # category -> sorted words (long first)；开启时加载
        self._sensitive_words_by_cat: dict[str, list[str]] = {}
        self._sensitive_word_count = 0
        self.sensitive_filter_enabled = self._load_sensitive_filter_enabled()
        if self.sensitive_filter_enabled:
            self._load_sensitive_lexicon()
        logger.info(
            "[kkt] 插件已加载: commands=/hajimi,/kkt,/image2 blacklist_count=%d "
            "model=%s image2_model=%s image2_key=%s main_keys=%d image2_keys=%d "
            "endpoint=%s image2_mode=%s image2_size=%s "
            "reply_with_quote=%s reaction_enabled=%s reaction_count=%d "
            "cooldown=%ds daily_quota=%d enable_at_avatar=%s label_images=%s "
            "prefer_chinese_text=%s prefer_cn_locale=%s style_prompt_len=%d "
            "sensitive_filter=%s sensitive_words=%d sensitive_cats=%s lexicon=%s",
            len(self.group_blacklist),
            self.model,
            self.image2_model,
            "set" if self.image2_api_key else "missing",
            len(self._build_key_chain(self.api_key, self.backup_api_keys)),
            len(
                self._build_key_chain(
                    self.image2_api_key, self.image2_backup_api_keys
                )
            ),
            f"{self.api_base}/chat/completions",
            self.image2_api_mode,
            self.image2_size,
            self.reply_with_quote,
            self.reaction_emoji_enabled,
            len(self.reaction_emoji_list),
            self.cooldown_seconds,
            self.daily_quota,
            self.enable_at_avatar,
            self.label_images,
            self.prefer_chinese_text,
            self.prefer_cn_locale,
            len(self.style_prompt),
            self.sensitive_filter_enabled,
            self._sensitive_word_count,
            sorted(self._sensitive_words_by_cat.keys()) or "(none)",
            str(self.sensitive_lexicon_path),
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

    @classmethod
    def _parse_category_list(cls, value) -> list[str]:
        """WebUI list / 逗号分隔 → 去重保序的类别名。"""
        if value is None:
            return []
        if isinstance(value, str):
            text = value.replace("，", ",").replace("\r\n", "\n").replace("\r", "\n")
            parts = re.split(r"[\n,;|]+", text)
        elif isinstance(value, list):
            parts = [str(item) for item in value]
        else:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for part in parts:
            name = str(part or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out

    def _resolve_lexicon_files(self) -> list[Path]:
        """解析要加载的 Vocabulary/*.txt；categories 空=全部已知映射文件。"""
        root = self.sensitive_lexicon_path
        vocab = root / "Vocabulary"
        if not vocab.is_dir():
            # 允许用户直接把 txt 放在词库根目录
            if root.is_dir():
                vocab = root
            else:
                return []
        selected = set(self.sensitive_categories)
        files: list[Path] = []
        for path in sorted(vocab.glob("*.txt")):
            cat = self._LEXICON_FILE_TO_CATEGORY.get(path.name)
            if cat is None:
                # 未知文件名：仅在「未选类别=全部」时加载
                if selected:
                    continue
                cat = path.stem
            if selected and cat not in selected:
                continue
            files.append(path)
        return files

    def _load_sensitive_lexicon(self) -> None:
        """从 Sensitive-lexicon 加载词条到内存（按类；词按长度降序便于先匹配长词）。"""
        by_cat: dict[str, set[str]] = {}
        files = self._resolve_lexicon_files()
        if not files:
            logger.warning(
                "[kkt] 敏感词过滤已开启但未找到词库文件: path=%s categories=%s "
                "请从 https://github.com/konsheng/Sensitive-lexicon 下载并放到该目录 "
                "(需含 Vocabulary/*.txt)",
                self.sensitive_lexicon_path,
                self.sensitive_categories or "(全部)",
            )
            self._sensitive_words_by_cat = {}
            self._sensitive_word_count = 0
            return
        for path in files:
            cat = self._LEXICON_FILE_TO_CATEGORY.get(path.name, path.stem)
            bucket = by_cat.setdefault(cat, set())
            try:
                text = path.read_text(encoding="utf-8-sig", errors="ignore")
            except OSError as exc:
                logger.warning("[kkt] 读取词库失败: file=%s err=%s", path, exc)
                continue
            for line in text.splitlines():
                word = line.strip()
                if not word or word.startswith("#"):
                    continue
                # 过短单字极易误杀，跳过长度 < 2
                if len(word) < 2:
                    continue
                bucket.add(word)
        self._sensitive_words_by_cat = {
            cat: sorted(words, key=len, reverse=True)
            for cat, words in by_cat.items()
            if words
        }
        self._sensitive_word_count = sum(
            len(words) for words in self._sensitive_words_by_cat.values()
        )
        logger.info(
            "[kkt] 敏感词库已加载: files=%d categories=%s words=%d path=%s",
            len(files),
            sorted(self._sensitive_words_by_cat.keys()),
            self._sensitive_word_count,
            self.sensitive_lexicon_path,
        )

    def _find_sensitive_hit(self, text: str) -> tuple[str, str] | None:
        """返回 (category, word)；未命中 None。先长词后短词。"""
        body = (text or "").strip()
        if not body or not self._sensitive_words_by_cat:
            return None
        # 统一小写便于匹配英文缩写
        haystack = body.casefold()
        for cat, words in self._sensitive_words_by_cat.items():
            for word in words:
                needle = word.casefold()
                if needle and needle in haystack:
                    return cat, word
        return None

    def _check_sensitive_prompt(self, prompt: str) -> str | None:
        """开启过滤时检查 prompt；命中则写日志并返回用户文案。"""
        if not self.sensitive_filter_enabled:
            return None
        if not self._sensitive_words_by_cat:
            return None
        hit = self._find_sensitive_hit(prompt)
        if not hit:
            return None
        cat, word = hit
        logger.warning(
            "[kkt] 敏感词拦截: category=%s keyword=%s prompt_length=%d",
            cat,
            word,
            len(prompt or ""),
        )
        return self._SENSITIVE_REJECT_USER_MSG

    def _load_sensitive_filter_enabled(self) -> bool:
        """读取运行时审核开关；无覆盖文件则用 WebUI 默认。"""
        try:
            path = self.sensitive_filter_override_path
            if not path.exists():
                return self._sensitive_filter_config_default
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "enabled" not in data:
                return self._sensitive_filter_config_default
            return bool(data.get("enabled"))
        except Exception as exc:
            logger.warning("[kkt] 读取审核开关覆盖失败，回退配置默认: %s", exc)
            return self._sensitive_filter_config_default

    def _save_sensitive_filter_enabled(self, enabled: bool) -> None:
        """持久化运行时审核开关（不改 WebUI 配置文件）。"""
        enabled = bool(enabled)
        payload = {
            "enabled": enabled,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "command",
        }
        self.sensitive_filter_override_path.parent.mkdir(parents=True, exist_ok=True)
        self.sensitive_filter_override_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.sensitive_filter_enabled = enabled
        if enabled and not self._sensitive_words_by_cat:
            self._load_sensitive_lexicon()
        logger.info(
            "[kkt] 本地审核开关已更新: enabled=%s words=%d",
            enabled,
            self._sensitive_word_count,
        )

    def _format_sensitive_status(self) -> str:
        """本地审核状态文案。"""
        state = "开" if self.sensitive_filter_enabled else "关"
        cats = sorted(self._sensitive_words_by_cat.keys())
        if self.sensitive_categories:
            cat_line = "、".join(self.sensitive_categories)
        elif cats:
            cat_line = "、".join(cats)
        else:
            cat_line = "（未加载）"
        lines = [
            f"本地审核：{state}",
            f"词条：{self._sensitive_word_count}",
            f"类别：{cat_line}",
        ]
        if self.sensitive_filter_enabled and self._sensitive_word_count <= 0:
            lines.append(
                "提示：词库未加载或为空，请检查 Sensitive-lexicon 目录"
            )
        lines.append("开关：/kkt审核 开|关（仅管理员）")
        return "\n".join(lines)

    @staticmethod
    def _parse_sensitive_toggle_arg(text: str) -> bool | None:
        """解析审核开关参数。空=查询；开/关=True/False；无法识别返回 None。"""
        raw = (text or "").strip()
        if not raw:
            return None
        normalized = re.sub(r"\s+", "", raw).casefold()
        # 去掉可能的前缀「审核」
        normalized = re.sub(
            r"^(?:审核|过滤|敏感词|sensitive|filter|moderation)",
            "",
            normalized,
            flags=re.IGNORECASE,
        )
        if normalized in {
            "开",
            "开启",
            "启用",
            "打开",
            "on",
            "enable",
            "enabled",
            "true",
            "1",
        }:
            return True
        if normalized in {
            "关",
            "关闭",
            "停用",
            "off",
            "disable",
            "disabled",
            "false",
            "0",
        }:
            return False
        # 整词就是 开/关 已覆盖；带前缀时上面已 strip
        if raw in {"开", "关"}:
            return raw == "开"
        return None

    @staticmethod
    def _parse_secret_list(value) -> list[str]:
        """解析备用 Key 列表；支持 list 或逗号/换行分隔字符串，保序去重。"""
        if value is None:
            return []
        if isinstance(value, str):
            text = value.replace("，", ",").replace("\r\n", "\n").replace("\r", "\n")
            parts = re.split(r"[\n,;|]+", text)
        elif isinstance(value, list):
            parts = [str(item) for item in value]
        else:
            parts = [str(value)]
        result: list[str] = []
        seen: set[str] = set()
        for part in parts:
            key = part.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(key)
        return result

    @staticmethod
    def _build_key_chain(primary: str, backups: list[str] | None = None) -> list[str]:
        """主 Key + 备用 Key，保序去重；空串忽略。"""
        chain: list[str] = []
        seen: set[str] = set()
        for key in [primary or "", *(backups or [])]:
            item = str(key or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            chain.append(item)
        return chain

    @staticmethod
    def _mask_secret(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return "empty"
        if len(text) <= 10:
            return text[:2] + "***"
        return f"{text[:6]}...{text[-4:]}"

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

    def _build_image_chain(
        self,
        event: AstrMessageEvent,
        image_path: str,
        elapsed_seconds: int | None = None,
    ) -> list:
        """组装出图消息链；可选前置 Reply，并附带耗时文案。"""
        chain: list = []
        if self.reply_with_quote:
            message_id = self._extract_reaction_message_id(event)
            if message_id is not None:
                chain.append(Comp.Reply(id=message_id))
            else:
                logger.debug("[kkt] 引用回复跳过: 无法获取 message_id")
        if elapsed_seconds is not None:
            chain.append(Comp.Plain(f"生成耗时：{elapsed_seconds}秒，请查收喵"))
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

    def _load_daily_quota_limit(self) -> int:
        """读取运行时日限额；无覆盖文件则用插件配置默认值。"""
        try:
            path = self.quota_limit_override_path
            if not path.exists():
                return self._daily_quota_config_default
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "limit" not in data:
                return self._daily_quota_config_default
            return max(0, int(data.get("limit")))
        except Exception as exc:
            logger.warning("[kkt] 读取日限额覆盖失败，回退配置默认: %s", exc)
            return self._daily_quota_config_default

    def _save_daily_quota_limit(self, limit: int) -> None:
        """持久化运行时日限额（不改 WebUI 配置文件，不重置已用次数）。"""
        limit = max(0, int(limit))
        payload = {
            "limit": limit,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "command",
        }
        self.quota_limit_override_path.parent.mkdir(parents=True, exist_ok=True)
        self.quota_limit_override_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.daily_quota = limit
        logger.info("[kkt] 日限额已更新: limit=%d", limit)

    @staticmethod
    def _parse_quota_limit_arg(text: str) -> int | None:
        """解析额度指令参数。

        支持：
        - 空 / 无数字 -> None（表示查询）
        - "10" / "额度 10" / "限额10" / "set 10" / "to10"
        返回非负整数；解析失败返回 None（调用方再判断是否非法）。
        """
        raw = (text or "").strip()
        if not raw:
            return None
        # 去掉常见前缀词，只留数字
        cleaned = re.sub(
            r"(?i)^\s*(?:额度|限额|配额|quota|limit|set|to|为|到|=|:|：)+",
            "",
            raw,
        ).strip()
        cleaned = re.sub(r"\s+", "", cleaned)
        if not cleaned:
            return None
        if re.fullmatch(r"\d+", cleaned):
            return int(cleaned)
        # 兜底：整段里抓第一个整数
        match = re.search(r"(\d+)", raw)
        if match and re.fullmatch(r"[\D]*\d+[\D]*", raw.replace(" ", "")):
            # 避免把「查看10次记录」这类误当 set；仅当文本基本是“词+数字”
            return int(match.group(1))
        return None

    def _format_quota_status(self, event: AstrMessageEvent | None = None) -> str:
        """生成限额状态文案（含日配额与当前用户 CD）。"""
        if self.daily_quota <= 0:
            used = int(self._load_quota_state().get("count") or 0)
            daily_line = "今日额度：不限制"
            if used:
                daily_line += f"（已用 {used}）"
        else:
            state = self._load_quota_state()
            used = int(state.get("count") or 0)
            remain = max(0, self.daily_quota - used)
            daily_line = f"今日额度：{used}/{self.daily_quota}（剩余 {remain}）"

        if self.cooldown_seconds <= 0:
            cd_line = "冷却：关闭"
        elif event is not None and self._is_admin(event):
            cd_line = f"冷却：{self.cooldown_seconds}s（管理员免冷却）"
        elif event is not None:
            sender_id = str(event.get_sender_id() or "").strip()
            last = self._user_last_call.get(sender_id) if sender_id else None
            if last is None:
                cd_line = f"冷却：{self.cooldown_seconds}s"
            else:
                remain_cd = self.cooldown_seconds - (time.monotonic() - last)
                if remain_cd > 0:
                    cd_line = f"冷却：还需 {int(remain_cd) + 1}s"
                else:
                    cd_line = f"冷却：{self.cooldown_seconds}s"
        else:
            cd_line = f"冷却：{self.cooldown_seconds}s"

        return f"{daily_line}\n{cd_line}"

    async def _set_daily_quota_limit(self, limit: int) -> dict:
        """设置日限额上限；不修改已用 count。"""
        async with self._quota_lock:
            self._save_daily_quota_limit(limit)
            state = self._load_quota_state()
            return {
                "limit": self.daily_quota,
                "used": int(state.get("count") or 0),
                "date": state.get("date") or date.today().isoformat(),
            }

    async def _reset_daily_quota(self) -> dict:
        """将今日已用次数清零。返回新状态。"""
        async with self._quota_lock:
            state = {"date": date.today().isoformat(), "count": 0}
            self._save_quota_state(state)
            logger.info("[kkt] 日配额已重置: %s", state)
            return state

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
                return f"今日额度已用完（{used}/{self.daily_quota}）"

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

    @filter.command(
        "kkt额度",
        alias={
            "hajimi额度",
            "image2额度",
            "kktquota",
            "hajimiquota",
            "image2quota",
            "kkt限额",
            "hajimi限额",
            "image2限额",
            "kkt配额",
            "hajimi配额",
            "image2配额",
        },
    )
    async def handle_quota_status(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        """查看或设置日配额。

        - /kkt额度 -> 查询
        - /kkt额度 10 -> 管理员将日上限改为 10，已用次数不变
        hajimi / image2 同义，三通道共用一套配额。
        """
        group_id = str(event.get_group_id() or "").strip()
        if group_id and group_id in self.group_blacklist:
            return
        event.stop_event()

        # 优先用框架解析的 arg；空则从整句再抽一次，兼容 /kkt额度10
        raw_arg = str(arg or "").strip()
        if not raw_arg:
            msg = (event.get_message_str() or "").strip()
            # 去掉命令前缀
            raw_arg = re.sub(
                r"(?i)^/?(?:kkt|hajimi|image2)(?:额度|限额|配额|quota)\s*",
                "",
                msg,
            ).strip()

        limit = self._parse_quota_limit_arg(raw_arg)
        # 有参数但解析不出合法数字
        if raw_arg and limit is None:
            yield event.plain_result("参数无效。查询：/kkt额度；设置：/kkt额度 10")
            return

        if limit is None:
            yield event.plain_result(self._format_quota_status(event))
            return

        if not self._is_admin(event):
            yield event.plain_result(
                "仅管理员可调整额度。\n" + self._format_quota_status(event)
            )
            return

        old_limit = self.daily_quota
        result = await self._set_daily_quota_limit(limit)
        used = int(result["used"])
        new_limit = int(result["limit"])
        if new_limit <= 0:
            head = f"已关闭日限额（已用 {used}）"
        else:
            remain = max(0, new_limit - used)
            head = f"日限额 {old_limit} → {new_limit}（已用 {used}，剩余 {remain}）"
        logger.info(
            "[kkt] 管理员调整日限额: operator=%s old=%d new=%d used=%d",
            event.get_sender_id(),
            old_limit,
            new_limit,
            used,
        )
        yield event.plain_result(head + "\n" + self._format_quota_status(event))

    @filter.command(
        "kkt重置额度",
        alias={
            "hajimi重置额度",
            "image2重置额度",
            "kktresetquota",
            "hajimiresetquota",
            "image2resetquota",
            "kkt清零额度",
            "hajimi清零额度",
            "image2清零额度",
        },
    )
    async def handle_quota_reset(self, event: AstrMessageEvent):
        """重置今日已用次数（仅管理员）；不改日限额上限。"""
        group_id = str(event.get_group_id() or "").strip()
        if group_id and group_id in self.group_blacklist:
            return
        event.stop_event()
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可重置额度。")
            return
        await self._reset_daily_quota()
        text = (
            f"已清零今日已用（上限 {self.daily_quota}）\n"
            + self._format_quota_status(event)
        )
        logger.info(
            "[kkt] 管理员重置已用次数: operator=%s limit=%d",
            event.get_sender_id(),
            self.daily_quota,
        )
        yield event.plain_result(text)

    @filter.command(
        "kkt审核",
        alias={
            "hajimi审核",
            "image2审核",
            "kkt过滤",
            "hajimi过滤",
            "image2过滤",
            "kktsensitive",
            "hajimisensitive",
            "image2sensitive",
        },
    )
    async def handle_sensitive_toggle(
        self, event: AstrMessageEvent, arg: GreedyStr = ""
    ):
        """查看或开关本地敏感词审核。

        - /kkt审核 -> 查询
        - /kkt审核 开|关 -> 仅管理员
        """
        group_id = str(event.get_group_id() or "").strip()
        if group_id and group_id in self.group_blacklist:
            return
        event.stop_event()

        raw_arg = str(arg or "").strip()
        if not raw_arg:
            msg = (event.get_message_str() or "").strip()
            raw_arg = re.sub(
                r"(?i)^/?(?:kkt|hajimi|image2)(?:审核|过滤|sensitive)\s*",
                "",
                msg,
            ).strip()

        if not raw_arg:
            yield event.plain_result(self._format_sensitive_status())
            return

        toggle = self._parse_sensitive_toggle_arg(raw_arg)
        if toggle is None:
            yield event.plain_result(
                "参数无效。查询：/kkt审核；开关：/kkt审核 开|关"
            )
            return

        if not self._is_admin(event):
            yield event.plain_result(
                "仅管理员可开关本地审核。\n" + self._format_sensitive_status()
            )
            return

        old = self.sensitive_filter_enabled
        self._save_sensitive_filter_enabled(toggle)
        head = f"本地审核：{'开' if old else '关'} → {'开' if toggle else '关'}"
        logger.info(
            "[kkt] 管理员切换本地审核: operator=%s old=%s new=%s words=%d",
            event.get_sender_id(),
            old,
            toggle,
            self._sensitive_word_count,
        )
        yield event.plain_result(head + "\n" + self._format_sensitive_status())

    def _detect_command_name(self, event: AstrMessageEvent) -> str:
        """从消息中识别触发的主指令名（hajimi / kkt / image2）。"""
        raw = (event.get_message_str() or "").strip()
        first = raw.split()[0] if raw.split() else ""
        first = first.lstrip("/").lower()
        if first.startswith("image2"):
            return "image2"
        if first.startswith("kkt"):
            return "kkt"
        if first.startswith("hajimi"):
            return "hajimi"
        # 兜底：Plain 拼接
        plain = "".join(
            getattr(c, "text", "") or ""
            for c in event.get_messages()
            if isinstance(c, Comp.Plain)
        ).strip()
        token = plain.split()[0].lstrip("/").lower() if plain.split() else ""
        if token.startswith("image2"):
            return "image2"
        if token.startswith("kkt"):
            return "kkt"
        return "hajimi"

    def _resolve_api_credentials(
        self, command: str
    ) -> tuple[str, list[str], str] | str:
        """返回 (api_base, api_keys[主+备], model)；失败返回错误文案。"""
        if command == "image2":
            keys = self._build_key_chain(
                self.image2_api_key, self.image2_backup_api_keys
            )
            if not keys:
                return (
                    "未配置 image2 专用 API Key。请在插件配置中填写 image2_api_key"
                    "（不会使用默认 api_key）。"
                )
            return self.image2_api_base, keys, self.image2_model
        keys = self._build_key_chain(self.api_key, self.backup_api_keys)
        if not keys:
            return (
                "未配置 NewAPI Key，请在 AstrBot WebUI 的 Hajimi 图片生成插件配置中填写 api_key。"
            )
        return self.api_base, keys, self.model

    @filter.command("hajimi", alias={"kkt", "image2"})
    async def handle_command(self, event: AstrMessageEvent, prompt: GreedyStr = ""):
        group_id = str(event.get_group_id() or "").strip()
        if group_id and group_id in self.group_blacklist:
            logger.debug("[kkt] 忽略黑名单群消息: group_id=%s", group_id)
            return

        command = self._detect_command_name(event)
        prompt = self._extract_prompt(event, prompt)
        logger.info(
            "[kkt] 指令匹配: command=%s prompt=%r",
            command,
            prompt[:200],
        )
        event.stop_event()

        # 兼容：/kkt 额度、/kkt 额度 10、/kkt 重置额度 写在主指令参数里
        # 三通道共用，prompt 来自 /kkt|/hajimi|/image2 参数部分
        prompt_stripped = (prompt or "").strip()
        normalized = re.sub(r"\s+", "", prompt_stripped.lower())
        # 查询
        if normalized in {
            "额度",
            "限额",
            "quota",
            "配额",
            "今日额度",
            "今日限额",
        }:
            yield event.plain_result(self._format_quota_status(event))
            return
        # 设置：额度10 / 额度 10 / quota10 / 限额=20
        set_match = re.fullmatch(
            r"(?:额度|限额|配额|quota|limit)\s*(?:=|：|:|为|到)?\s*(\d+)",
            prompt_stripped,
            flags=re.IGNORECASE,
        )
        if set_match:
            if not self._is_admin(event):
                yield event.plain_result(
                    "仅管理员可调整额度。\n" + self._format_quota_status(event)
                )
                return
            new_limit = int(set_match.group(1))
            old_limit = self.daily_quota
            result = await self._set_daily_quota_limit(new_limit)
            used = int(result["used"])
            if new_limit <= 0:
                head = f"已关闭日限额（已用 {used}）"
            else:
                remain = max(0, new_limit - used)
                head = f"日限额 {old_limit} → {new_limit}（已用 {used}，剩余 {remain}）"
            logger.info(
                "[kkt] 管理员调整日限额(主指令参数): operator=%s old=%d new=%d used=%d",
                event.get_sender_id(),
                old_limit,
                new_limit,
                used,
            )
            yield event.plain_result(head + "\n" + self._format_quota_status(event))
            return
        if normalized in {
            "重置额度",
            "清零额度",
            "resetquota",
            "重置配额",
            "清零配额",
            "reset",
        }:
            if not self._is_admin(event):
                yield event.plain_result("仅管理员可重置额度。")
                return
            await self._reset_daily_quota()
            yield event.plain_result(
                f"已清零今日已用（上限 {self.daily_quota}）\n"
                + self._format_quota_status(event)
            )
            return

        # 兼容：/kkt 审核、/kkt 审核 开|关
        if normalized in {"审核", "过滤", "sensitive", "moderation"}:
            yield event.plain_result(self._format_sensitive_status())
            return
        sens_match = re.fullmatch(
            r"(?:审核|过滤|sensitive|moderation)\s*(?:=|：|:)?\s*(.+)",
            prompt_stripped,
            flags=re.IGNORECASE,
        )
        if sens_match:
            toggle = self._parse_sensitive_toggle_arg(sens_match.group(1))
            if toggle is None:
                yield event.plain_result(
                    "参数无效。查询：/kkt审核；开关：/kkt审核 开|关"
                )
                return
            if not self._is_admin(event):
                yield event.plain_result(
                    "仅管理员可开关本地审核。\n"
                    + self._format_sensitive_status()
                )
                return
            old = self.sensitive_filter_enabled
            self._save_sensitive_filter_enabled(toggle)
            head = (
                f"本地审核：{'开' if old else '关'} → {'开' if toggle else '关'}"
            )
            logger.info(
                "[kkt] 管理员切换本地审核(主指令参数): operator=%s old=%s new=%s",
                event.get_sender_id(),
                old,
                toggle,
            )
            yield event.plain_result(head + "\n" + self._format_sensitive_status())
            return

        # 先收集引用图文，再判断 help。
        # 否则裸 /kkt + 引用文案 会在读引用之前就被当成空 prompt 返回帮助。
        try:
            image_items, quoted_prompt = await self._collect_images(event)
        except Exception as exc:
            logger.warning(f"[kkt] 读取引用内容失败: {exc}")
            image_items, quoted_prompt = [], ""

        if quoted_prompt:
            prompt = f"{quoted_prompt}\n{prompt}".strip() if prompt else quoted_prompt
        elif self._is_help_token(prompt):
            prompt = ""

        logger.info(
            "[kkt] 输入解析完成: command=%s prompt_length=%d image_count=%d quoted_prompt=%s labels=%s",
            command,
            len(prompt),
            len(image_items),
            bool(quoted_prompt),
            [item.get("label") for item in image_items][:8],
        )
        if not prompt and not image_items:
            yield event.plain_result(self._help_text)
            return

        # 本地 Sensitive-lexicon：三通道共用；命中则不请求、不扣配额
        sensitive_msg = self._check_sensitive_prompt(prompt)
        if sensitive_msg:
            yield event.plain_result(sensitive_msg)
            return

        creds = self._resolve_api_credentials(command)
        if isinstance(creds, str):
            logger.error("[kkt] 凭证未配置: command=%s", command)
            yield event.plain_result(creds)
            return
        api_base, api_keys, model = creds

        # image2 + Images API：多参考图会静默丢弃，直接拦截以免浪费额度
        if (
            command == "image2"
            and len(image_items) > 1
            and self._should_use_images_api(command, model)
        ):
            reject_msg = self._format_image2_multi_ref_reject(image_items)
            logger.info(
                "[kkt] image2 多参考图拦截(不请求): count=%d labels=%s",
                len(image_items),
                [item.get("label") for item in image_items][:8],
            )
            yield event.plain_result(reject_msg)
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
        # 普通消息提示进度，不引用原消息
        await event.send(event.plain_result("正在生成图片，马上就好喵"))
        started_at = time.monotonic()

        try:
            logger.info(
                "[kkt] 开始调用图像 API: command=%s model=%s key_count=%d "
                "image_count=%d label_images=%s image2_mode=%s",
                command,
                model,
                len(api_keys),
                len(image_items),
                self.label_images,
                self.image2_api_mode if command == "image2" else "n/a",
            )
            result = await self._request_image(
                prompt,
                image_items,
                event,
                api_base=api_base,
                api_keys=api_keys,
                model=model,
                command=command,
            )
            if not result:
                logger.error("[kkt] API 调用完成但未解析出图片")
                yield event.plain_result("API 返回中没有找到图片，请检查模型和接口响应格式。")
                return
            image_path = await self._materialize_image(result)
            if not image_path:
                yield event.plain_result("图片下载或解析失败，请稍后重试。")
                return
            elapsed_seconds = max(1, int(round(time.monotonic() - started_at)))
            logger.info(
                "[kkt] 图片处理成功: path=%s elapsed=%ss",
                image_path,
                elapsed_seconds,
            )
            yield event.chain_result(
                self._build_image_chain(
                    event,
                    image_path,
                    elapsed_seconds=elapsed_seconds,
                )
            )
            self._schedule_cleanup(image_path)
        except Exception as exc:
            logger.error(f"[kkt] 图片生成失败: {exc}")
            err_text = str(exc).strip() or "未知错误"
            if err_text.startswith("上游拒绝生成："):
                yield event.plain_result(
                    err_text[len("上游拒绝生成：") :].strip() or err_text
                )
            else:
                yield event.plain_result(f"图片生成失败：{err_text}")

    async def _collect_images(
        self, event: AstrMessageEvent
    ) -> tuple[list[dict], str]:
        """收集参考图，带 source/label 元数据。

        顺序固定：引用图 → 当前消息图 → @头像。
        每项: {data_url, source, qq?, name?}
        """
        images: list[dict] = []
        seen: set[str] = set()
        quoted_texts: list[str] = []

        async def add_image(
            component,
            *,
            source: str,
            qq: str | None = None,
            name: str | None = None,
        ) -> None:
            if not isinstance(component, Comp.Image):
                return
            value = getattr(component, "url", None) or getattr(component, "file", None)
            if not value or value in seen:
                return
            seen.add(value)
            try:
                encoded = await component.convert_to_base64()
            except Exception as exc:
                logger.warning(f"[kkt] 图片转 Base64 失败: {exc}")
                return
            images.append(
                {
                    "data_url": f"data:image/jpeg;base64,{encoded}",
                    "source": source,
                    "qq": qq,
                    "name": name,
                }
            )

        current_images: list = []
        quoted_images: list = []
        for component in event.get_messages():
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

        for component in quoted_images:
            await add_image(component, source="quote")
        for component in current_images:
            await add_image(component, source="message")

        # enable_at_avatar：全部 @ 头像，可与引用图/当前图合并
        if self.enable_at_avatar:
            seen_qq: set[str] = set()
            try:
                self_id = str(event.get_self_id() or "").strip()
            except Exception:
                self_id = ""
            for component in event.get_messages():
                if not isinstance(component, Comp.At):
                    continue
                qq = getattr(component, "qq", None) or getattr(component, "target", None)
                if qq is None or str(qq) in {"", "0", "all"}:
                    continue
                qq_str = str(qq).strip()
                if not qq_str or qq_str in seen_qq:
                    continue
                if self_id and qq_str == self_id:
                    continue
                seen_qq.add(qq_str)
                display = (
                    getattr(component, "name", None)
                    or getattr(component, "nickname", None)
                    or getattr(component, "display_name", None)
                    or qq_str
                )
                avatar = Comp.Image.fromURL(
                    f"https://q1.qlogo.cn/g?b=qq&nk={qq_str}&s=640"
                )
                await add_image(
                    avatar,
                    source="avatar",
                    qq=qq_str,
                    name=str(display).strip() or qq_str,
                )
            if seen_qq:
                logger.info(
                    "[kkt] 收集 @ 头像: count=%d qqs=%s total_images=%d",
                    len(seen_qq),
                    list(seen_qq)[:10],
                    len(images),
                )

        # 编号标签（1-based）
        for index, item in enumerate(images, start=1):
            item["index"] = index
            item["label"] = self._image_label(item, index)

        return images, "\n".join(quoted_texts)

    @staticmethod
    def _image_label(item: dict, index: int) -> str:
        source = item.get("source") or "image"
        if source == "quote":
            role = "引用原图/底图"
        elif source == "message":
            role = "当前消息图片"
        elif source == "avatar":
            name = item.get("name") or item.get("qq") or "用户"
            role = f"@{name} 的头像"
        else:
            role = "参考图"
        return f"图{index} · {role}"

    @classmethod
    def _rewrite_prompt_with_image_refs(
        cls, prompt: str, image_items: list[dict]
    ) -> str:
        """把 @昵称 / 图片N 改写成 图N，方便模型对齐。"""
        text = (prompt or "").strip()
        if not text or not image_items:
            return text

        # 1) 用户口头「图片1/图2」→「图1/图2」
        def repl_num(match: re.Match) -> str:
            return f"图{int(match.group(1))}"

        text = cls._USER_IMAGE_REF_RE.sub(repl_num, text)

        # 2) 头像图：按 qq / 昵称 把 @xxx 换成 图N（@xxx）
        # 先替换更长的昵称，避免短名误伤
        avatar_items = [
            item for item in image_items if item.get("source") == "avatar"
        ]
        avatar_items.sort(
            key=lambda it: len(str(it.get("name") or it.get("qq") or "")),
            reverse=True,
        )
        for item in avatar_items:
            index = item.get("index")
            qq = str(item.get("qq") or "").strip()
            name = str(item.get("name") or "").strip()
            if not index:
                continue
            patterns: list[str] = []
            if qq:
                patterns.append(re.escape(f"@{qq}"))
                patterns.append(re.escape(f"@{name}({qq})") if name else "")
                patterns.append(rf"@[\w\u4e00-\u9fff\-·.]+\({re.escape(qq)}\)")
            if name and name != qq:
                patterns.append(re.escape(f"@{name}"))
            for pat in patterns:
                if not pat:
                    continue
                text = re.sub(
                    pat,
                    f"图{index}（@{name or qq}）",
                    text,
                    count=1,
                )
        return text

    def _compose_user_instruction(self, prompt: str) -> str:
        """用户指令 + 可选预制风格/中文约束。"""
        body = (prompt or "").strip() or "请生成一张图片"
        if self.style_prompt:
            return f"{self.style_prompt}\n\n用户指令：{body}"
        return f"用户指令：{body}" if prompt else body

    def _build_multimodal_content(
        self,
        prompt: str,
        image_items: list[dict],
        event: AstrMessageEvent | None = None,
    ) -> list[dict]:
        """组装发给模型的 content：可选【图N】标签与图片交错。"""
        rewritten = self._rewrite_prompt_with_image_refs(prompt, image_items)
        # 再剥一层可能残留的纯 @token（未映射成功时）
        if event is not None and rewritten.startswith("@"):
            rewritten = self._strip_at_tokens(rewritten) or rewritten

        content: list[dict] = []
        use_labels = self.label_images and len(image_items) >= 1
        instruction = self._compose_user_instruction(rewritten)

        if use_labels and image_items:
            content.append(
                {
                    "type": "text",
                    "text": (
                        "以下按顺序给出参考图片。每张图前有【图N · 说明】标签；"
                        "请严格按标签理解人物/物体对应关系，并按用户指令生成或编辑图片。"
                    ),
                }
            )
            for item in image_items:
                label = item.get("label") or f"图{item.get('index', '?')}"
                content.append({"type": "text", "text": f"【{label}】"})
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": item["data_url"]},
                    }
                )
            content.append({"type": "text", "text": instruction})
        else:
            content.append({"type": "text", "text": instruction})
            for item in image_items:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": item["data_url"]},
                    }
                )
        return content

    def _should_use_images_api(self, command: str, model: str) -> bool:
        """仅 /image2 可能走 images API；/kkt /hajimi 始终 chat。"""
        if command != "image2":
            return False
        mode = self.image2_api_mode
        if mode == "images":
            return True
        if mode == "chat":
            return False
        # auto：按模型名猜测（gpt-image / dall-e 走 images）
        name = (model or "").lower()
        return any(
            token in name
            for token in (
                "gpt-image",
                "dall-e",
                "dalle",
                "imagen",  # 少数中转也挂在 images
            )
        )

    @staticmethod
    def _format_image2_multi_ref_reject(image_items: list[dict]) -> str:
        """images 模式多参考图拦截文案：不请求 API、不扣额度。"""
        total = len(image_items or [])
        lines = [
            f"/image2 当前为 Images edit模式，只支持 1 张参考图+文字说明（已收到 {total} 张）。"
            "请只保留一张再试；多图合图请用哈基米 /hajimi。"
        ]
        labels = [
            str(item.get("label") or "").strip()
            for item in (image_items or [])
            if str(item.get("label") or "").strip()
        ]
        if labels:
            lines.append("已识别：")
            for label in labels[:8]:
                lines.append(f"· {label}")
            if total > 8:
                lines.append(f"· …共 {total} 张")
        return "\n".join(lines)

    @staticmethod
    def _data_url_to_bytes(data_url: str) -> tuple[bytes, str, str]:
        """返回 (raw_bytes, mime, filename_suffix)。"""
        text = (data_url or "").strip()
        if not text.startswith("data:"):
            raise RuntimeError("参考图不是 data URL，无法提交到 images/edits")
        header, encoded = text.split(",", 1)
        mime = "image/png"
        if ";" in header:
            mime = header[5:].split(";", 1)[0] or mime
        elif header.startswith("data:"):
            mime = header[5:] or mime
        ext = {
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
        }.get(mime.lower(), "png")
        return base64.b64decode(encoded), mime, ext

    def _compose_images_prompt(self, prompt: str) -> str:
        """images API 用纯文本 prompt（可带 style 软约束）。"""
        body = (prompt or "").strip() or "请生成一张图片"
        if self.style_prompt:
            return f"{self.style_prompt}\n\n用户指令：{body}"
        return body

    async def _request_image(
        self,
        prompt: str,
        image_items: list[dict],
        event: AstrMessageEvent | None = None,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        api_keys: list[str] | None = None,
        model: str | None = None,
        command: str = "kkt",
    ) -> str | None:
        use_base = (api_base or self.api_base).rstrip("/")
        use_model = model or self.model
        key_chain = self._build_key_chain(
            "",
            list(api_keys or []) + ([api_key] if api_key else []),
        )
        if not key_chain:
            # 仅作为兜底：正常路径应已由 _resolve_api_credentials 给出 keys
            if command == "image2":
                key_chain = self._build_key_chain(
                    self.image2_api_key, self.image2_backup_api_keys
                )
            else:
                key_chain = self._build_key_chain(self.api_key, self.backup_api_keys)
        if not key_chain:
            raise RuntimeError("未配置可用 API Key")

        # 关键隔离：只有 image2 且配置/模型需要时才走 images API
        if self._should_use_images_api(command, use_model):
            return await self._request_image_via_images_api(
                prompt,
                image_items,
                api_base=use_base,
                api_keys=key_chain,
                model=use_model,
            )
        return await self._request_image_via_chat(
            prompt,
            image_items,
            event,
            api_base=use_base,
            api_keys=key_chain,
            model=use_model,
        )

    async def _request_image_via_images_api(
        self,
        prompt: str,
        image_items: list[dict],
        *,
        api_base: str,
        api_keys: list[str],
        model: str,
    ) -> str | None:
        """/image2 专用：文生图 generations，图生图 edits。不影响 /kkt。"""
        use_base = api_base.rstrip("/")
        text_prompt = self._compose_images_prompt(prompt)
        has_ref = bool(image_items)
        if has_ref and len(image_items) > 1:
            logger.warning(
                "[kkt] image2/images 多图仅使用第 1 张: total=%d labels=%s",
                len(image_items),
                [item.get("label") for item in image_items][:6],
            )

        endpoint = (
            f"{use_base}/images/edits"
            if has_ref
            else f"{use_base}/images/generations"
        )
        last_error = "未知错误"
        logger.info(
            "[kkt] image2 images 请求准备: endpoint=%s model=%s key_count=%d "
            "ref_images=%d prompt_length=%d size=%s",
            endpoint,
            model,
            len(api_keys),
            len(image_items),
            len(text_prompt),
            self.image2_size,
        )

        ref_bytes = ref_mime = ref_ext = None
        if has_ref:
            ref_bytes, ref_mime, ref_ext = self._data_url_to_bytes(
                str(image_items[0].get("data_url") or "")
            )

        for key_index, use_key in enumerate(api_keys):
            key_label = "primary" if key_index == 0 else f"backup#{key_index}"
            key_mask = self._mask_secret(use_key)
            logger.info(
                "[kkt] image2 使用 Key: index=%d/%d role=%s mask=%s",
                key_index + 1,
                len(api_keys),
                key_label,
                key_mask,
            )
            for attempt in range(self.max_retry + 1):
                try:
                    timeout = aiohttp.ClientTimeout(total=self.timeout)
                    headers = {"Authorization": f"Bearer {use_key}"}
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        logger.info(
                            "[kkt] image2 API 发送: key=%s attempt=%d/%d endpoint=%s",
                            key_label,
                            attempt + 1,
                            self.max_retry + 1,
                            endpoint,
                        )
                        if has_ref:
                            form = aiohttp.FormData()
                            form.add_field("model", model)
                            form.add_field("prompt", text_prompt)
                            form.add_field("n", "1")
                            if self.image2_size:
                                form.add_field("size", self.image2_size)
                            form.add_field(
                                "image",
                                ref_bytes,
                                filename=f"input.{ref_ext}",
                                content_type=ref_mime or "image/png",
                            )
                            # 部分兼容层同时认 image[] / response_format
                            form.add_field("response_format", "b64_json")
                            post_cm = session.post(endpoint, headers=headers, data=form)
                        else:
                            payload = {
                                "model": model,
                                "prompt": text_prompt,
                                "n": 1,
                            }
                            if self.image2_size:
                                payload["size"] = self.image2_size
                            # 优先要 base64，避免临时 URL 过期；不支持时上游会忽略
                            payload["response_format"] = "b64_json"
                            headers = {
                                **headers,
                                "Content-Type": "application/json",
                            }
                            post_cm = session.post(
                                endpoint, headers=headers, json=payload
                            )

                        async with post_cm as response:
                            raw = await response.text()
                            logger.info(
                                "[kkt] image2 API 响应: key=%s attempt=%d status=%d bytes=%d",
                                key_label,
                                attempt + 1,
                                response.status,
                                len(raw),
                            )
                            image, err = self._handle_images_http_response(
                                response.status, raw
                            )
                            if image:
                                logger.info(
                                    "[kkt] image2 图片解析成功: key=%s source=%s",
                                    key_label,
                                    "data_url" if image.startswith("data:") else "url",
                                )
                                return image
                            last_error = err or "API 响应中未找到图片"
                            raise RuntimeError(last_error)
                except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                    last_error = str(exc)
                    logger.error(
                        "[kkt] image2 API 失败: key=%s attempt=%d error=%s",
                        key_label,
                        attempt + 1,
                        last_error[:300],
                    )
                    no_retry = self._is_non_retryable_api_error(last_error)
                    switch_key = self._should_switch_api_key(last_error)
                    if no_retry and not switch_key:
                        raise RuntimeError(last_error) from exc
                    if switch_key:
                        break
                    if attempt < self.max_retry and not no_retry:
                        await asyncio.sleep(self.retry_delay)
                        continue
                    break

            if key_index + 1 < len(api_keys):
                logger.warning(
                    "[kkt] image2 切换备用 Key: from=%s next_index=%d/%d last_error=%s",
                    key_label,
                    key_index + 2,
                    len(api_keys),
                    last_error[:200],
                )
                if self.retry_delay > 0:
                    await asyncio.sleep(self.retry_delay)
                continue
            raise RuntimeError(last_error)
        return None

    def _handle_images_http_response(
        self, status: int, raw: str
    ) -> tuple[str | None, str | None]:
        """解析 images/* HTTP 响应；返回 (image, error_message)。"""
        if status in {401, 403}:
            return None, "API Key 无效或没有模型权限"
        if status == 429:
            return None, "API 请求频率或额度受限"
        if status >= 500:
            detail = (raw or "")[:220].replace("\n", " ")
            return None, (
                f"上游服务异常 HTTP {status}: {detail}"
                if detail
                else f"上游服务异常 HTTP {status}"
            )
        if status >= 400:
            return None, f"API 请求失败 HTTP {status}: {(raw or '')[:200]}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None, f"API 返回非 JSON：{(raw or '')[:200]}"
        image = self._extract_image(data)
        if image:
            return image, None
        # images 错误体有时 200 包 error
        if isinstance(data, dict) and data.get("error"):
            err = data.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or err)
            else:
                msg = str(err)
            return None, f"上游返回错误：{msg[:300]}"
        return None, self._format_missing_image_error(
            data if isinstance(data, dict) else {}
        )

    @staticmethod
    def _is_non_retryable_api_error(message: str) -> bool:
        text = message or ""
        return (
            "Key 无效" in text
            or "权限" in text
            or "上游拒绝生成" in text
            or "模型未返回图片，而是回复了文字" in text
            or "only supported on" in text.lower()
            or "不支持" in text
        )

    @staticmethod
    def _should_switch_api_key(message: str) -> bool:
        text = (message or "").lower()
        # 认证失败 / 无渠道 / 额度：切备用 Key 更有意义
        return (
            "key 无效" in (message or "")
            or "权限" in (message or "")
            or "no available channel" in text
            or "model_not_found" in text
            or "额度" in (message or "")
            or "429" in text
            or "频率" in (message or "")
        )

    async def _request_image_via_chat(
        self,
        prompt: str,
        image_items: list[dict],
        event: AstrMessageEvent | None = None,
        *,
        api_base: str,
        api_keys: list[str],
        model: str,
    ) -> str | None:
        """原 chat/completions 路径：供 /kkt /hajimi，以及 image2_mode=chat 使用。"""
        content = self._build_multimodal_content(prompt, image_items, event)
        image_count = sum(
            1
            for part in content
            if isinstance(part, dict) and part.get("type") == "image_url"
        )

        use_base = api_base.rstrip("/")
        use_model = model
        key_chain = list(api_keys)
        payload = {
            "model": use_model,
            "messages": [{"role": "user", "content": content}],
            "temperature": self.temperature,
        }
        endpoint = f"{use_base}/chat/completions"
        last_error = "未知错误"
        logger.debug(
            "[kkt] chat 请求准备: endpoint=%s model=%s key_count=%d "
            "prompt_length=%d image_count=%d content_parts=%d payload_bytes≈%d",
            endpoint,
            use_model,
            len(key_chain),
            len(prompt),
            image_count,
            len(content),
            len(json.dumps(payload, ensure_ascii=False)),
        )

        for key_index, use_key in enumerate(key_chain):
            key_label = "primary" if key_index == 0 else f"backup#{key_index}"
            key_mask = self._mask_secret(use_key)
            headers = {
                "Authorization": f"Bearer {use_key}",
                "Content-Type": "application/json",
            }
            logger.info(
                "[kkt] 使用 Key: index=%d/%d role=%s mask=%s",
                key_index + 1,
                len(key_chain),
                key_label,
                key_mask,
            )
            key_failed_hard = False
            for attempt in range(self.max_retry + 1):
                try:
                    timeout = aiohttp.ClientTimeout(total=self.timeout)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        logger.info(
                            "[kkt] API 请求发送: key=%s attempt=%d/%d",
                            key_label,
                            attempt + 1,
                            self.max_retry + 1,
                        )
                        async with session.post(
                            endpoint, headers=headers, json=payload
                        ) as response:
                            raw = await response.text()
                            logger.info(
                                "[kkt] API 响应: key=%s attempt=%d status=%d bytes=%d",
                                key_label,
                                attempt + 1,
                                response.status,
                                len(raw),
                            )
                            if response.status in {401, 403}:
                                last_error = "API Key 无效或没有模型权限"
                                key_failed_hard = True
                                raise RuntimeError(last_error)
                            if response.status == 429:
                                last_error = "API 请求频率或额度受限"
                                raise RuntimeError(last_error)
                            if response.status >= 500:
                                detail = raw[:220].replace("\n", " ")
                                last_error = (
                                    f"上游服务异常 HTTP {response.status}: {detail}"
                                    if detail
                                    else f"上游服务异常 HTTP {response.status}"
                                )
                                raise RuntimeError(last_error)
                            if response.status >= 400:
                                raise RuntimeError(
                                    f"API 请求失败 HTTP {response.status}: {raw[:200]}"
                                )
                            try:
                                data = json.loads(raw)
                            except json.JSONDecodeError as exc:
                                raise RuntimeError(
                                    f"API 返回非 JSON：{raw[:200]}"
                                ) from exc
                            image = self._extract_image(data)
                            if image:
                                logger.info(
                                    "[kkt] API 图片解析成功: key=%s source=%s",
                                    key_label,
                                    "data_url" if image.startswith("data:") else "url",
                                )
                                return image
                            text_reply = self._extract_text_reply(data)
                            finish = self._extract_finish_reason(data)
                            logger.error(
                                "[kkt] API JSON 未找到图片: key=%s top_keys=%s choices=%d "
                                "finish_reason=%s text_preview=%r raw_preview=%r",
                                key_label,
                                list(data)[:20]
                                if isinstance(data, dict)
                                else type(data).__name__,
                                len(data.get("choices", []))
                                if isinstance(data, dict)
                                else 0,
                                finish,
                                (text_reply or "")[:300],
                                raw[:300],
                            )
                            raise RuntimeError(
                                self._format_missing_image_error(
                                    data if isinstance(data, dict) else {},
                                    text_reply=text_reply,
                                    finish_reason=finish,
                                )
                            )
                except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                    last_error = str(exc)
                    logger.error(
                        "[kkt] API 请求失败: key=%s attempt=%d error=%s",
                        key_label,
                        attempt + 1,
                        last_error[:300],
                    )
                    if (
                        "上游拒绝生成" in last_error
                        or "模型未返回图片，而是回复了文字" in last_error
                    ):
                        raise RuntimeError(last_error) from exc
                    if key_failed_hard or self._should_switch_api_key(last_error):
                        # no channel / 401 等：不必在同 Key 内耗完重试
                        if (
                            "no available channel" in last_error.lower()
                            or "model_not_found" in last_error.lower()
                            or key_failed_hard
                        ):
                            break
                    if attempt < self.max_retry and not self._is_non_retryable_api_error(
                        last_error
                    ):
                        await asyncio.sleep(self.retry_delay)
                        continue
                    break

            if key_index + 1 < len(key_chain):
                logger.warning(
                    "[kkt] 主/当前 Key 失败，切换备用 Key: from=%s next_index=%d/%d last_error=%s",
                    key_label,
                    key_index + 2,
                    len(key_chain),
                    last_error[:200],
                )
                if self.retry_delay > 0:
                    await asyncio.sleep(self.retry_delay)
                continue
            raise RuntimeError(last_error)
        return None

    @classmethod
    def _is_empty_upstream_response(cls, data: dict) -> bool:
        """Detect empty shell response (content null/empty)."""
        if not isinstance(data, dict):
            return False
        message = cls._message_from_response(data)
        if not message:
            choices = data.get("choices")
            return True if choices is not None else not bool(data)
        content = message.get("content", None)
        if content is None:
            return True
        if isinstance(content, str) and not content.strip():
            return True
        if isinstance(content, list) and len(content) == 0:
            return True
        return False

    @classmethod
    def _format_missing_image_error(
        cls,
        data: dict,
        *,
        text_reply: str | None = None,
        finish_reason: str | None = None,
    ) -> str:
        """User-facing missing-image error."""
        reply = (
            text_reply
            if text_reply is not None
            else cls._extract_text_reply(data)
        ).strip()
        finish = (
            finish_reason
            if finish_reason is not None
            else cls._extract_finish_reason(data)
        )
        if reply:
            return f"上游拒绝生成：{reply[:400]}"
        if cls._is_empty_upstream_response(data):
            suffix = f"（finish_reason={finish}）" if finish else ""
            return (
                "上游空响应（content 为空），可能被安全拦截，请换提示词后重试"
                + suffix
            )
        suffix = f"（finish_reason={finish}）" if finish else ""
        return "API 响应中未找到图片" + suffix

    @staticmethod
    def _message_from_response(data: dict) -> dict:
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            return {}
        first = choices[0] if isinstance(choices, list) else {}
        if not isinstance(first, dict):
            return {}
        message = first.get("message") or first.get("delta") or {}
        return message if isinstance(message, dict) else {}

    @classmethod
    def _extract_finish_reason(cls, data: dict) -> str:
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices or not isinstance(choices, list):
            return ""
        first = choices[0] if choices else {}
        if not isinstance(first, dict):
            return ""
        return str(first.get("finish_reason") or first.get("finishReason") or "")

    @classmethod
    def _extract_text_reply(cls, data: dict) -> str:
        """从 chat/completions 响应中提取纯文本（无图时用于提示用户）。"""
        message = cls._message_from_response(data)
        chunks: list[str] = []

        def collect(value) -> None:
            if value is None:
                return
            if isinstance(value, str):
                text = value.strip()
                # 跳过 data-url / 裸 base64 噪声
                if text.startswith("data:image"):
                    return
                if len(text) > 80 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", text):
                    return
                if text:
                    chunks.append(text)
                return
            if isinstance(value, list):
                for item in value:
                    collect(item)
                return
            if isinstance(value, dict):
                # 常见多模态 content part
                part_type = value.get("type")
                if part_type in {"text", "input_text", "output_text"}:
                    collect(value.get("text") or value.get("content"))
                    return
                if "text" in value and part_type not in {
                    "image_url",
                    "image",
                    "input_image",
                }:
                    collect(value.get("text"))
                # refusal 字段（部分兼容层）
                if value.get("refusal"):
                    collect(value.get("refusal"))

        collect(message.get("content"))
        collect(message.get("refusal"))
        # 少数网关把说明放在顶层
        if isinstance(data, dict):
            collect(data.get("error"))
            err = data.get("error")
            if isinstance(err, dict):
                collect(err.get("message"))

        # 去重保序
        ordered: list[str] = []
        for item in chunks:
            if item not in ordered:
                ordered.append(item)
        return "\n".join(ordered).strip()

    @classmethod
    def _extract_image(cls, data: dict) -> str | None:
        message = cls._message_from_response(data)

        def from_url_candidate(value) -> str | None:
            if not isinstance(value, str):
                return None
            value = value.strip()
            if not value:
                return None
            if value.startswith("data:image/"):
                return value
            if value.startswith("http://") or value.startswith("https://"):
                # 避免把普通网页链接当图；有扩展名或常见图床路径更可信
                lower = value.lower().split("?", 1)[0]
                if any(
                    lower.endswith(ext)
                    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
                ) or any(
                    token in lower
                    for token in (
                        "/image",
                        "img",
                        "pic",
                        "cdn",
                        "oss",
                        "cos.",
                        "s3.",
                    )
                ):
                    return value
                # 仍接受 http(s)，兼容无扩展名的临时图链
                return value
            return None

        # 1) message.images（NewAPI / Gemini 兼容常见字段）
        for item in message.get("images") or []:
            if isinstance(item, str):
                found = from_url_candidate(item)
                if found:
                    return found
                continue
            if not isinstance(item, dict):
                continue
            for key in ("url", "image_url", "b64_json", "data"):
                raw_val = item.get(key)
                if key == "image_url" and isinstance(raw_val, dict):
                    raw_val = raw_val.get("url")
                if key in {"b64_json", "data"} and isinstance(raw_val, str) and raw_val:
                    if raw_val.startswith("data:image/"):
                        return raw_val
                    return f"data:image/png;base64,{raw_val}"
                found = from_url_candidate(raw_val) if isinstance(raw_val, str) else None
                if found:
                    return found

        # 2) content 列表 / 字符串
        content = message.get("content")
        parts = content if isinstance(content, list) else [content]
        for part in parts:
            if isinstance(part, dict):
                part_type = part.get("type")
                if part_type in {"image_url", "image", "input_image", "output_image"}:
                    value = part.get("image_url") or part.get("url") or part.get("image")
                    if isinstance(value, dict):
                        value = value.get("url") or value.get("data")
                    if isinstance(value, str):
                        if value.startswith("data:image/") or value.startswith("http"):
                            found = from_url_candidate(value)
                            if found:
                                return found
                        # 纯 base64
                        if len(value) > 64 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", value or ""):
                            return f"data:image/png;base64,{value.strip()}"
                # inline_data / Gemini 风格
                inline = part.get("inline_data") or part.get("inlineData")
                if isinstance(inline, dict):
                    blob = inline.get("data")
                    mime = inline.get("mime_type") or inline.get("mimeType") or "image/png"
                    if isinstance(blob, str) and blob:
                        if blob.startswith("data:image/"):
                            return blob
                        return f"data:{mime};base64,{blob}"
            if isinstance(part, str):
                match = re.search(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", part)
                if match:
                    return match.group(0)
                # markdown 图片
                match = re.search(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", part)
                if match:
                    found = from_url_candidate(match.group(1))
                    if found:
                        return found
                match = re.search(r"https?://[^\s)>'\"]+", part)
                if match:
                    found = from_url_candidate(match.group(0))
                    if found:
                        return found

        # 3) 顶层 data[]（少数 images 接口混在 chat 里）
        if isinstance(data, dict):
            for item in data.get("data") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("b64_json"):
                    return f"data:image/png;base64,{item['b64_json']}"
                found = from_url_candidate(item.get("url") or "")
                if found:
                    return found
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
