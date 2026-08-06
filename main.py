"""AstrBot plugin for NewAPI image generation and editing."""

import asyncio
import base64
import io
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from random import choice
from typing import ClassVar
from urllib.parse import urlparse

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from PIL import Image

try:
    from .video_client import (
        GrokVideoClient,
        GrokVideoError,
        normalize_api_base,
        resolve_media_url,
    )
    from .web_api import KktWebApiMixin
except ImportError:  # pragma: no cover - flat `import main` in offline tests
    from video_client import (  # type: ignore
        GrokVideoClient,
        GrokVideoError,
        normalize_api_base,
        resolve_media_url,
    )
    from web_api import KktWebApiMixin  # type: ignore


_COMMAND_CANONICALS = {
    "main": "hajimi",
    "image2": "image2",
    "grok": "grok",
    "grok2": "grok2",
    "video": "grokvideo",
    "main_gif": "hajimigif",
    "main_gif2": "hajimigif2",
    "image2_gif": "image2gif",
    "image2_gif2": "image2gif2",
    "kkgifzip": "kkgifzip",
    "grokpack": "grokpack",
    "grokvg": "grokvg",
}

_DEFAULT_COMMAND_ALIASES = {
    "main": ["kkt"],
    "image2": [],
    "grok": ["gk"],
    "grok2": ["grok2k", "gk2", "gk2k"],
    "video": ["grokv", "gkv", "gv"],
    "main_gif": ["kktgif"],
    "main_gif2": [],
    "image2_gif": [],
    "image2_gif2": [],
    "kkgifzip": ["gifz", "gifzip"],
    # 工作流：全套 / 视频+GIF；z 系别名在 map 里再扩 1-5 档
    "grokpack": [
        "gkpack",
        "gkp",
        "grokpackz",
        "gkpackz",
        "gkpz",
    ],
    "grokvg": [
        "gkvg",
        "gvg",
        "grokvgz",
        "gkvgz",
        "gvgz",
    ],
}

_DEFAULT_HELP_ALIASES = {
    "hajimi帮助",
    "image2帮助",
    "grok帮助",
    "grok2帮助",
    "grokvideo帮助",
    "kkthelp",
    "hajimihelp",
    "image2help",
    "grokhelp",
    "grok2help",
    "grokvideohelp",
}

_DEFAULT_ADMIN_COMMAND_NAMES = {
    "quota": [
        "kkt额度",
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
        "kkt统计",
        "hajimi统计",
        "image2统计",
    ],
    "reset": [
        "kkt重置额度",
        "hajimi重置额度",
        "image2重置额度",
        "kktresetquota",
        "hajimiresetquota",
        "image2resetquota",
        "kkt清零额度",
        "hajimi清零额度",
        "image2清零额度",
    ],
    "moderation": [
        "kkt审核",
        "hajimi审核",
        "image2审核",
        "kkt过滤",
        "hajimi过滤",
        "image2过滤",
        "kktsensitive",
        "hajimisensitive",
        "image2sensitive",
    ],
    "help": ["kkt帮助", *_DEFAULT_HELP_ALIASES],
}


def _expand_video_aliases(names: list[str] | set[str]) -> set[str]:
    """Add the compact ``alias5`` duration spelling used by video commands."""
    expanded = {str(name).strip() for name in names if str(name).strip()}
    expanded.update(
        f"{name}{duration}"
        for name in list(expanded)
        for duration in range(100)
    )
    return expanded


_DEFAULT_VIDEO_FILTER_ALIASES = _expand_video_aliases(_DEFAULT_COMMAND_ALIASES["video"])


def _format_command_names(names: list[str] | tuple[str, ...]) -> str:
    return "|".join(f"/{name}" for name in names if str(name).strip())


def _default_help_groups() -> dict[str, dict[str, object]]:
    groups = {
        "main": {
            "label": "主图像",
            "names": [_COMMAND_CANONICALS["main"], *_DEFAULT_COMMAND_ALIASES["main"]],
            "description": "文生图、修图、多图参考和引用图编辑",
        },
        "image2": {
            "label": "Image2",
            "names": [_COMMAND_CANONICALS["image2"]],
            "description": "独立 Image2 通道；Images 模式最多一张参考图",
        },
        "grok": {
            "label": "Grok 生图",
            "names": [_COMMAND_CANONICALS["grok"], *_DEFAULT_COMMAND_ALIASES["grok"]],
            "description": "Grok Images 文生图/图生图，支持多图参考",
        },
        "grok2": {
            "label": "Grok 2K",
            "names": [_COMMAND_CANONICALS["grok2"], *_DEFAULT_COMMAND_ALIASES["grok2"]],
            "description": "Grok 2K 文生图，不接受参考图",
        },
        "video": {
            "label": "Grok 视频",
            "names": [_COMMAND_CANONICALS["video"], *_DEFAULT_COMMAND_ALIASES["video"]],
            "description": "文生/图生视频；可在指令中写 1-15 秒",
        },
    }
    groups.update(
        {
            "admin_quota": {
                "label": "额度",
                "names": _DEFAULT_ADMIN_COMMAND_NAMES["quota"],
                "description": "管理员查询或设置三条通道日配额。",
            },
            "admin_reset": {
                "label": "重置额度",
                "names": _DEFAULT_ADMIN_COMMAND_NAMES["reset"],
                "description": "管理员清零今日已用次数，累计次数保留。",
            },
            "admin_moderation": {
                "label": "审核",
                "names": _DEFAULT_ADMIN_COMMAND_NAMES["moderation"],
                "description": "查询或切换本地敏感词审核。",
            },
            "help": {
                "label": "帮助",
                "names": _DEFAULT_ADMIN_COMMAND_NAMES["help"],
                "description": "显示当前全部 canonical 指令和别名。",
            },
        }
    )
    return groups


def _help_group_names(
    groups: dict[str, dict[str, object]],
    key: str,
    fallback: list[str] | None = None,
) -> list[str]:
    item = groups.get(key) or {}
    names = [str(name) for name in item.get("names", []) if str(name).strip()]
    return names or list(fallback or [])


def _help_is_noise_alias(name: str) -> bool:
    """Skip admin/help synonym spam in user-facing alias lists."""
    n = str(name or "").casefold()
    if not n:
        return True
    noise = (
        "帮助",
        "help",
        "额度",
        "限额",
        "配额",
        "统计",
        "quota",
        "重置",
        "reset",
        "清零",
        "审核",
        "过滤",
        "sensitive",
    )
    return any(token in n for token in noise)


def _help_short_aliases(
    names: list[str], *, max_n: int = 5, skip_z_bases: bool = False
) -> list[str]:
    """Up to max_n short daily-use aliases (skip primary)."""
    if not names:
        return []
    out: list[str] = []
    for name in names[1:]:
        if _help_is_noise_alias(name):
            continue
        folded = name.casefold()
        # 跳过时长/档位数字展开（grokv5、gkpz3）
        if re.fullmatch(r".+\d+", name):
            continue
        # 工作流主行不列 z 基名（另有压缩行）
        if skip_z_bases and (folded.endswith("z") or folded.endswith("zip")):
            if folded not in {"gifz", "gifzip", "kkgifzip"}:
                continue
        out.append(name)
        if len(out) >= max_n:
            break
    return out


def _help_cmd_line(
    names: list[str],
    desc: str,
    *,
    max_alias: int = 5,
    skip_z_bases: bool = False,
) -> str:
    if not names:
        return ""
    primary = f"/{names[0]}"
    aliases = _help_short_aliases(
        names, max_n=max_alias, skip_z_bases=skip_z_bases
    )
    if aliases:
        alias_part = " · ".join(f"/{a}" for a in aliases)
        return f"{primary} · {alias_part}  {desc}"
    return f"{primary}  {desc}"


def build_user_help_markdown(
    command_groups: dict[str, dict[str, object]] | None = None,
) -> str:
    """Markdown help card for T2I（含常用别名，无内部实现话术）。"""
    groups = command_groups or _default_help_groups()
    g = lambda key, fb: _help_group_names(groups, key, fb)  # noqa: E731

    lines = [
        "# 康康图",
        "",
        "## 生图",
        _help_cmd_line(g("main", ["hajimi", "kkt"]), "文生图 / 修图 / 参考图"),
        _help_cmd_line(g("grok", ["grok", "gk"]), "Grok 生图"),
        _help_cmd_line(g("grok2", ["grok2", "grok2k", "gk2"]), "2K 文生图"),
        _help_cmd_line(g("image2", ["image2"]), "独立通道"),
        "",
        "## 视频与 GIF",
        _help_cmd_line(
            g("video", ["grokvideo", "grokv", "gkv", "gv"]),
            "文/图生视频（可 /gv5 指定秒数）",
        ),
        _help_cmd_line(g("main_gif", ["hajimigif", "kktgif"]), "16 帧分镜 GIF"),
        _help_cmd_line(g("main_gif2", ["hajimigif2"]), "9 帧分镜 GIF"),
        _help_cmd_line(g("image2_gif", ["image2gif"]), "Image2 · 16 帧"),
        _help_cmd_line(g("image2_gif2", ["image2gif2"]), "Image2 · 9 帧"),
        "/kkgif  视频转 GIF",
        _help_cmd_line(
            g("kkgifzip", ["kkgifzip", "gifz", "gifzip"]),
            "压缩 GIF（/gifz3 = 3 档）",
        ),
        "",
        "## 工作流",
        _help_cmd_line(
            g("grokpack", ["grokpack", "gkpack", "gkp"]),
            "图 → 视频 → GIF",
            skip_z_bases=True,
        ),
        "/grokpackz · /gkpz1-5  同上，成品压缩",
        _help_cmd_line(
            g("grokvg", ["grokvg", "gkvg", "gvg"]),
            "视频 → GIF",
            skip_z_bases=True,
        ),
        "/grokvgz · /gvgz1-5  同上，成品压缩",
        "",
        "工作流提示词 = 视频意图；全套会先出首帧图。",
        "完成后：一条过程合并转发 + 一条最终 GIF。",
        "",
        "## 管理",
        "/kkt额度  /kkt重置额度  /kkt审核",
        "/kkt帮助",
    ]
    return "\n".join(line for line in lines if line is not None)


def build_user_help_plain(
    command_groups: dict[str, dict[str, object]] | None = None,
) -> str:
    """Plain-text fallback with the same structure (no markdown headers)."""
    groups = command_groups or _default_help_groups()
    g = lambda key, fb: _help_group_names(groups, key, fb)  # noqa: E731
    lines = [
        "康康图",
        "",
        "【生图】",
        _help_cmd_line(g("main", ["hajimi", "kkt"]), "文生图/修图"),
        _help_cmd_line(g("grok", ["grok", "gk"]), "Grok 生图"),
        _help_cmd_line(g("grok2", ["grok2", "grok2k", "gk2"]), "2K 文生图"),
        _help_cmd_line(g("image2", ["image2"]), "独立通道"),
        "",
        "【视频 / GIF】",
        _help_cmd_line(
            g("video", ["grokvideo", "grokv", "gkv", "gv"]),
            "文/图生视频（/gv5=秒数）",
        ),
        _help_cmd_line(g("main_gif", ["hajimigif", "kktgif"]), "16 帧分镜"),
        _help_cmd_line(g("main_gif2", ["hajimigif2"]), "9 帧分镜"),
        _help_cmd_line(g("image2_gif", ["image2gif"]), "Image2 16 帧"),
        _help_cmd_line(g("image2_gif2", ["image2gif2"]), "Image2 9 帧"),
        "/kkgif  视频转 GIF",
        _help_cmd_line(
            g("kkgifzip", ["kkgifzip", "gifz", "gifzip"]),
            "压缩（/gifz3）",
        ),
        "",
        "【工作流】",
        _help_cmd_line(
            g("grokpack", ["grokpack", "gkpack", "gkp"]),
            "图→视频→GIF",
            skip_z_bases=True,
        ),
        "/grokpackz · /gkpz1-5  压缩成品",
        _help_cmd_line(
            g("grokvg", ["grokvg", "gkvg", "gvg"]),
            "视频→GIF",
            skip_z_bases=True,
        ),
        "/grokvgz · /gvgz1-5  压缩成品",
        "",
        "【管理】/kkt额度  /kkt重置额度  /kkt审核",
    ]
    return "\n".join(lines)


def build_basic_help_text(
    command_groups: dict[str, dict[str, object]] | None = None,
) -> str:
    """User-facing help plain text（含常用别名）。"""
    return build_user_help_plain(command_groups)


def build_workflow_help_text(
    command_groups: dict[str, dict[str, object]] | None = None,
) -> str:
    """Workflow-only plain block（兼容旧调用）。"""
    groups = command_groups or _default_help_groups()
    pack = _help_group_names(groups, "grokpack", ["grokpack", "gkp"])
    vg = _help_group_names(groups, "grokvg", ["grokvg", "gvg"])
    return (
        "【工作流】\n"
        f"{_help_cmd_line(pack, '图→视频→GIF')}\n"
        "/grokpackz · /gkpz1-5  压缩成品\n"
        f"{_help_cmd_line(vg, '视频→GIF')}\n"
        "/grokvgz · /gvgz1-5  压缩成品"
    )


def build_alias_help_text(
    command_groups: dict[str, dict[str, object]] | None = None,
) -> str:
    """常用别名速查（不含管理/帮助同义词洪流）。"""
    groups = command_groups or _default_help_groups()
    lines = ["【常用别名】"]
    for key, fallback in (
        ("main", ["hajimi", "kkt"]),
        ("grok", ["grok", "gk"]),
        ("grok2", ["grok2", "grok2k", "gk2"]),
        ("image2", ["image2"]),
        ("video", ["grokvideo", "grokv", "gkv", "gv"]),
        ("main_gif", ["hajimigif", "kktgif"]),
        ("kkgifzip", ["kkgifzip", "gifz", "gifzip"]),
        ("grokpack", ["grokpack", "gkpack", "gkp"]),
        ("grokvg", ["grokvg", "gkvg", "gvg"]),
    ):
        names = _help_group_names(groups, key, fallback)
        if not names:
            continue
        aliases = _help_short_aliases(names, max_n=5)
        if not aliases:
            lines.append(f"/{names[0]}")
        else:
            lines.append(
                f"/{names[0]} → " + " ".join(f"/{a}" for a in aliases)
            )
    lines.append("视频：/别名5 = 秒数；压缩/工作流 z：/别名3 = 档位")
    return "\n".join(lines)


def build_help_text(command_groups: dict[str, dict[str, object]] | None = None) -> str:
    """Full plain help (T2I 失败时的完整回退)。"""
    return build_user_help_plain(command_groups)


def build_gif_help_text(command_groups: dict[str, dict[str, object]] | None = None) -> str:
    """Build the GIF help text, including configured aliases."""
    groups = command_groups or _default_help_groups()

    def names(key: str, fallback: list[str]) -> list[str]:
        item = groups.get(key) or {}
        return [str(name) for name in item.get("names", [])] or fallback

    return (
        "康康动图\n"
        f"主通道 16 帧：{_format_command_names(names('main_gif', ['hajimigif', 'kktgif']))}\n"
        f"主通道 9 帧：{_format_command_names(names('main_gif2', ['hajimigif2']))}\n"
        f"Image2 16/9 帧：{_format_command_names(names('image2_gif', ['image2gif']))} / "
        f"{_format_command_names(names('image2_gif2', ['image2gif2']))}\n"
        "视频转 GIF：引用或附带一个视频后使用 /kkgif\n"
        "压缩：/kkgifzip|/gifz|/gifzip 或带档位 1-5（视频或 GIF；静态图不支持）\n"
        "每个分镜指令都支持提示词；无提示词时由模型选择简单循环动作。"
    )


def build_video_help_text(video_names: list[str] | None = None) -> str:
    """Build the help text for the canonical Grok video command."""
    names = video_names or ["grokvideo", "grokv", "gkv", "gv"]
    command_text = _format_command_names(names)
    return (
        "康康视频（grok2api）\n"
        f"用法：{command_text} <提示词>\n"
        f"图生：附图或回复图后使用 {command_text.split('|', 1)[0]} [提示词]\n"
        "仅支持 1 张参考图；可写 1-15 秒，如 /grokvideo 5 猫在雨中奔跑\n"
        "视频增强、时长、比例、分辨率和并发在插件配置中调整\n"
        "额度：/kkt额度 video（管理员）"
    )


@register(
    "astrbot_plugin_kkt",
    "konley",
    "调用 NewAPI 生图/修图，并对接 grok2api 视频",
    "0.19.1",
)
class KktImagePlugin(Star, KktWebApiMixin):
    """Generate or edit images through an OpenAI-compatible endpoint."""

    _GROK_IMAGE_MODEL = "grok-imagine-image-quality"

    # 计费/限额通道：kkt+hajimi 共用 main；image2 / video 独立
    _CHANNEL_MAIN = "main"
    _CHANNEL_IMAGE2 = "image2"
    _CHANNEL_VIDEO = "video"
    _CHANNELS = (_CHANNEL_MAIN, _CHANNEL_IMAGE2, _CHANNEL_VIDEO)
    _CHANNEL_LABELS: ClassVar[dict[str, str]] = {
        "main": "/hajimi",
        "image2": "/image2",
        "video": "/grokvideo",
    }
    _COMMAND_CANONICALS: ClassVar[dict[str, str]] = _COMMAND_CANONICALS
    _DEFAULT_COMMAND_ALIASES: ClassVar[dict[str, list[str]]] = _DEFAULT_COMMAND_ALIASES
    _COMMAND_ALIAS_FIELDS: ClassVar[dict[str, str]] = {
        "main": "main_command_aliases",
        "image2": "image2_command_aliases",
        "grok": "grok_command_aliases",
        "grok2": "grok2_command_aliases",
        "video": "video_command_aliases",
        "main_gif": "main_gif_aliases",
        "main_gif2": "main_gif2_aliases",
        "image2_gif": "image2_gif_aliases",
        "image2_gif2": "image2_gif2_aliases",
        "kkgifzip": "kkgifzip_aliases",
        "grokpack": "grokpack_aliases",
        "grokvg": "grokvg_aliases",
    }
    _COMMAND_HANDLER_NAMES: ClassVar[dict[str, str]] = {
        "main": "handle_hajimi",
        "image2": "handle_image2",
        "grok": "handle_grok",
        "grok2": "handle_grok2",
        "video": "handle_grokv",
        "main_gif": "handle_hajimigif",
        "main_gif2": "handle_hajimigif2",
        "image2_gif": "handle_image2gif",
        "image2_gif2": "handle_image2gif2",
        "kkgifzip": "handle_kkgifzip",
        "grokpack": "handle_grokpack",
        "grokvg": "handle_grokvg",
    }
    # 工作流 z 系：名字里带 z 的才扩 1-5 档
    _WORKFLOW_ZIP_BASES: ClassVar[frozenset[str]] = frozenset(
        {
            "grokpackz",
            "gkpackz",
            "gkpz",
            "grokvgz",
            "gkvgz",
            "gvgz",
        }
    )
    _HELP_HANDLER_NAME = "handle_help"

    # Sensitive-lexicon Vocabulary 文件名 → WebUI 可选类别名
    _LEXICON_FILE_TO_CATEGORY: ClassVar[dict[str, str]] = {
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
    # kkgifzip/gifz/gifzip 及其 1-5 档写在前，避免被更短 token 抢匹配
    _CMD_ARG_RE = re.compile(
        r"^/?(?:grokpackz[1-5]?|gkpackz[1-5]?|gkpz[1-5]?|grokpack|gkpack|gkp|grokvgz[1-5]?|gkvgz[1-5]?|gvgz[1-5]?|grokvg|gkvg|gvg|kkgifzip[1-5]?|gifzip[1-5]?|gifz[1-5]?|kkgif|grokvideo\d+|grokv\d+|gkv\d+|gv\d+|grokvideo|grokv|gkv|gv|grok2k|gk2k|gk2|grok2|grok|gk|image2gif2|image2gif|hajimigif2|hajimigif|kktgif|hajimi|kkt|image2)(?:帮助|help|\?)?(?:\s+|$)(.*)$",
        re.IGNORECASE | re.DOTALL,
    )
    # /kkgifzip 五档：固定 ~10fps；减 blur + 提饱和，降低发灰
    # crush：先缩到 dim/crush 再用 neighbor 放大；blur：gblur sigma
    _KKGIFZIP_PRESETS: ClassVar[dict[int, dict[str, float | int | str]]] = {
        1: {
            "dimension": 220,
            "fps": 10,
            "colors": 192,
            "crush": 2.0,
            "blur": 0.15,
            "saturation": 1.2,
            "dither": "bayer:bayer_scale=2",
        },
        2: {
            "dimension": 180,
            "fps": 10,
            "colors": 160,
            "crush": 2.4,
            "blur": 0.25,
            "saturation": 1.25,
            "dither": "bayer:bayer_scale=2",
        },
        3: {
            "dimension": 150,
            "fps": 10,
            "colors": 128,
            "crush": 2.8,
            "blur": 0.35,
            "saturation": 1.3,
            "dither": "bayer:bayer_scale=3",
        },
        4: {
            "dimension": 120,
            "fps": 10,
            "colors": 96,
            "crush": 3.2,
            "blur": 0.45,
            "saturation": 1.35,
            "dither": "none",
        },
        5: {
            "dimension": 100,
            "fps": 10,
            "colors": 72,
            "crush": 3.8,
            "blur": 0.55,
            "saturation": 1.4,
            "dither": "none",
        },
    }
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
        self.config = config or {}
        config = self._flatten_plugin_config(self.config)
        self._command_aliases = self._load_command_aliases(config)
        self._command_alias_map = self._build_command_alias_map()
        self._apply_configured_command_aliases()
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
        # /grokvideo：默认复用 Grok 生图通道；填写视频字段即可单独覆盖。
        configured_video_base = normalize_api_base(
            str(config.get("video_api_base", "") or "").strip()
        )
        configured_video_key = str(config.get("video_api_key", "") or "").strip()
        configured_video_backups = self._parse_secret_list(
            config.get("video_backup_api_keys", [])
        )
        self.video_model = str(
            config.get("video_model", "Web/grok-imagine-video") or "Web/grok-imagine-video"
        ).strip()
        self.video_duration = max(1, min(15, int(config.get("video_duration", 8) or 8)))
        ar = str(config.get("video_aspect_ratio", "16:9") or "16:9").strip()
        self.video_aspect_ratio = (
            ar
            if ar in {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}
            else "16:9"
        )
        res = str(config.get("video_resolution", "720p") or "720p").strip().lower()
        self.video_resolution = res if res in {"480p", "720p", "1080p"} else "720p"
        self.video_poll_interval = max(1, min(30, int(config.get("video_poll_interval", 3) or 3)))
        self.video_timeout = max(60, min(7200, int(config.get("video_timeout", 600) or 600)))
        self.video_max_concurrent = max(
            1, min(20, int(config.get("video_max_concurrent", 2) or 2))
        )
        self.video_max_concurrent_per_user = max(
            1, min(5, int(config.get("video_max_concurrent_per_user", 1) or 1))
        )
        self.video_cooldown_seconds = max(
            0, int(config.get("video_cooldown_seconds", 60) or 0)
        )
        self.cost_video_usd = max(0.0, float(config.get("cost_video_usd", 0) or 0))
        self.video_cleanup_delay = max(
            5, int(config.get("video_cleanup_delay", 60) or 60)
        )
        # /grokv 静态提示词前缀（与生图 style_prompt 分离）
        self.video_prompt_enhance = bool(config.get("video_prompt_enhance", True))
        self.video_style_prompt = str(
            config.get("video_style_prompt", "") or ""
        ).strip()
        frame_mode = str(
            config.get("animated_reference_frame", "首帧") or "首帧"
        ).strip()
        self.animated_reference_frame = (
            frame_mode if frame_mode in {"首帧", "中间帧", "末帧"} else "首帧"
        )
        # /grok 生图优先使用自身配置，留空时复用已配置的视频 Grok 通道，最后才回退主图像通道。
        grok_base = str(config.get("grok_api_base", "") or "").strip()
        self.grok_api_base = (
            normalize_api_base(grok_base)
            or configured_video_base
            or normalize_api_base(self.api_base)
            or self.api_base
        )
        self.grok_api_key = (
            str(config.get("grok_api_key", "") or "").strip()
            or configured_video_key
            or self.api_key
        )
        configured_grok_backups = self._parse_secret_list(
            config.get("grok_backup_api_keys", [])
        )
        self.grok_backup_api_keys = (
            configured_grok_backups
            or list(configured_video_backups)
            or list(self.backup_api_keys)
        )
        # 视频通道的地址、Key 和备用 Key 留空时复用 Grok 生图通道。
        self.video_api_base = configured_video_base or self.grok_api_base
        self.video_api_key = configured_video_key or self.grok_api_key
        self.video_backup_api_keys = configured_video_backups or list(
            self.grok_backup_api_keys
        )
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
        # 分通道日限额配置默认；运行时可由管理员指令覆盖
        # daily_quota 兼容旧配置，作为 main 默认；image2 可用独立项
        legacy_quota = max(0, int(config.get("daily_quota", 50)))
        main_cfg = config.get("daily_quota_main", None)
        image2_cfg = config.get("daily_quota_image2", None)
        video_cfg = config.get("daily_quota_video", None)
        self._daily_quota_config_defaults: dict[str, int] = {
            self._CHANNEL_MAIN: (
                max(0, int(main_cfg)) if main_cfg is not None else legacy_quota
            ),
            self._CHANNEL_IMAGE2: (
                max(0, int(image2_cfg)) if image2_cfg is not None else legacy_quota
            ),
            self._CHANNEL_VIDEO: (
                max(0, int(video_cfg)) if video_cfg is not None else 10
            ),
        }
        # 预估单价 USD/次（仅展示，非上游真实账单）
        self.cost_main_usd = max(0.0, float(config.get("cost_main_usd", 0) or 0))
        self.cost_image2_usd = max(0.0, float(config.get("cost_image2_usd", 0) or 0))
        self.cleanup_delay = max(5, int(config.get("cleanup_delay", 15)))
        self.temp_dir = Path(get_astrbot_data_path()) / "plugin_data" / "kkt"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        # 旧单桶文件；迁移后仍可读
        self.quota_path = self.temp_dir / "daily_quota.json"
        self.quota_limit_override_path = self.temp_dir / "daily_quota_limit.json"
        self.usage_path = self.temp_dir / "usage.json"
        self.task_log_path = self.temp_dir / "task_log.json"
        self._task_logs = self._load_task_logs()
        self.channel_limit_override_path = self.temp_dir / "channel_quota_limit.json"
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
        # 内存：sender_id -> 上次成功触发的 monotonic 时间
        self._user_last_call: dict[str, float] = {}
        self._video_user_last_call: dict[str, float] = {}
        self._video_user_inflight: dict[str, int] = {}
        self._video_global_inflight = 0
        self._video_slot_lock = asyncio.Lock()
        self._quota_lock = asyncio.Lock()
        self.channel_limits = self._load_channel_limits()
        # 兼容旧代码/测试：daily_quota ≈ main 通道日限
        self.daily_quota = self.channel_limits[self._CHANNEL_MAIN]
        command_groups = self._command_help_groups()
        self._help_text = build_help_text(command_groups)
        self._gif_help_text = build_gif_help_text(command_groups)
        self._video_help_text = build_video_help_text(
            self._command_names_for_key("video")
        )
        self.gif_frame_size = max(
            128, min(512, int(config.get("gif_frame_size", 256)))
        )
        self.gif_fps = max(1, min(20, int(config.get("gif_fps", 8))))
        self.gif_max_bytes = max(
            256 * 1024,
            min(15 * 1024 * 1024, int(config.get("gif_max_bytes", 8 * 1024 * 1024))),
        )
        self.video_gif_max_duration = max(
            1, min(16, int(config.get("video_gif_max_duration", 16) or 16))
        )
        self.video_gif_max_dimension = max(
            256, min(640, int(config.get("video_gif_max_dimension", 480) or 480))
        )
        self.video_gif_fps = max(
            5, min(15, int(config.get("video_gif_fps", 10) or 10))
        )
        self.video_gif_max_bytes = max(
            512 * 1024,
            min(15 * 1024 * 1024, int(config.get("video_gif_max_bytes", 8 * 1024 * 1024) or 8 * 1024 * 1024)),
        )
        # category -> sorted words (long first)；开启时加载
        self._sensitive_words_by_cat: dict[str, list[str]] = {}
        self._sensitive_word_count = 0
        self.sensitive_filter_enabled = self._load_sensitive_filter_enabled()
        if self.sensitive_filter_enabled:
            self._load_sensitive_lexicon()
        video_keys = self._build_key_chain(
            self.video_api_key, self.video_backup_api_keys
        )
        logger.info(
            "[kkt] 插件已加载: commands=/hajimi,/image2,/grok,/grok2,/grokvideo,/kkgif,/kkgifzip,/grokpack,/grokvg "
            "blacklist_count=%d model=%s grok_model=%s image2_model=%s image2_key=%s "
            "main_keys=%d grok_keys=%d image2_keys=%d video_keys=%d "
            "endpoint=%s image2_mode=%s image2_size=%s "
            "video_base=%s video_model=%s video_dur=%ds video_ar=%s video_res=%s "
            "video_max_concurrent=%d video_per_user=%d video_cd=%ds video_timeout=%ds "
            "reply_with_quote=%s reaction_enabled=%s reaction_count=%d "
            "cooldown=%ds quota_main=%d quota_image2=%d quota_video=%d "
            "cost_main=$%.4f cost_image2=$%.4f cost_video=$%.4f "
            "enable_at_avatar=%s label_images=%s "
            "prefer_chinese_text=%s prefer_cn_locale=%s style_prompt_len=%d "
            "video_prompt_enhance=%s video_style_prompt_len=%d "
            "sensitive_filter=%s sensitive_words=%d sensitive_cats=%s lexicon=%s",
            len(self.group_blacklist),
            self.model,
            self._GROK_IMAGE_MODEL,
            self.image2_model,
            "set" if self.image2_api_key else "missing",
            len(self._build_key_chain(self.api_key, self.backup_api_keys)),
            len(self._build_key_chain(self.grok_api_key, self.grok_backup_api_keys)),
            len(
                self._build_key_chain(
                    self.image2_api_key, self.image2_backup_api_keys
                )
            ),
            len(video_keys),
            f"{self.api_base}/chat/completions",
            self.image2_api_mode,
            self.image2_size,
            self.video_api_base or "(unset)",
            self.video_model,
            self.video_duration,
            self.video_aspect_ratio,
            self.video_resolution,
            self.video_max_concurrent,
            self.video_max_concurrent_per_user,
            self.video_cooldown_seconds,
            self.video_timeout,
            self.reply_with_quote,
            self.reaction_emoji_enabled,
            len(self.reaction_emoji_list),
            self.cooldown_seconds,
            self.channel_limits[self._CHANNEL_MAIN],
            self.channel_limits[self._CHANNEL_IMAGE2],
            self.channel_limits.get(self._CHANNEL_VIDEO, 0),
            self.cost_main_usd,
            self.cost_image2_usd,
            self.cost_video_usd,
            self.enable_at_avatar,
            self.label_images,
            self.prefer_chinese_text,
            self.prefer_cn_locale,
            len(self.style_prompt),
            self.video_prompt_enhance,
            len(self.video_style_prompt),
            self.sensitive_filter_enabled,
            self._sensitive_word_count,
            sorted(self._sensitive_words_by_cat.keys()) or "(none)",
            str(self.sensitive_lexicon_path),
        )
        self._cleanup_stale_files()
        self._register_webui_apis()

    @staticmethod
    def _flatten_plugin_config(config: dict) -> dict:
        """Accept grouped WebUI config while keeping flat config compatibility."""
        flattened: dict = {}
        for key, value in config.items():
            if isinstance(value, dict):
                flattened.update(value)
            else:
                flattened[key] = value
        return flattened

    @classmethod
    def _parse_command_alias_values(cls, value) -> list[str]:
        """Parse a WebUI alias list into valid single-token command names."""
        result: list[str] = []
        seen: set[str] = set()
        for raw in cls._parse_secret_list(value):
            name = str(raw or "").strip().lstrip("/")
            if not name or not re.fullmatch(r"[\w\u4e00-\u9fff-]+", name, re.UNICODE):
                continue
            folded = name.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            result.append(name)
        return result

    @classmethod
    def _load_command_aliases(cls, config: dict) -> dict[str, list[str]]:
        """Merge built-in aliases with user aliases while avoiding collisions."""
        aliases: dict[str, list[str]] = {}
        canonical_names = {
            str(name).casefold() for name in cls._COMMAND_CANONICALS.values()
        }
        used = set(canonical_names)
        ordered_keys = tuple(cls._COMMAND_ALIAS_FIELDS)
        for key in ordered_keys:
            candidates = [
                *cls._DEFAULT_COMMAND_ALIASES.get(key, []),
                *cls._parse_command_alias_values(
                    config.get(cls._COMMAND_ALIAS_FIELDS[key], [])
                ),
            ]
            current_canonical = cls._COMMAND_CANONICALS[key].casefold()
            selected: list[str] = []
            for alias in candidates:
                folded = alias.casefold()
                if folded == current_canonical or folded in used:
                    if folded not in used:
                        used.add(folded)
                    continue
                selected.append(alias)
                used.add(folded)
            aliases[key] = selected
        return aliases

    def _build_command_alias_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for key, canonical in self._COMMAND_CANONICALS.items():
            names = [canonical, *self._command_aliases.get(key, [])]
            for name in names:
                mapping[name.casefold()] = key
            if key == "video":
                for name in names:
                    for duration in range(100):
                        mapping[f"{name}{duration}".casefold()] = key
            if key == "kkgifzip":
                for name in names:
                    for level in range(1, 6):
                        mapping[f"{name}{level}".casefold()] = key
            if key in {"grokpack", "grokvg"}:
                for name in names:
                    folded = name.casefold()
                    if folded in self._WORKFLOW_ZIP_BASES or folded.endswith("z"):
                        for level in range(1, 6):
                            mapping[f"{folded}{level}"] = key
        mapping["kkgif"] = "kkgif"
        return mapping

    def _command_names_for_key(
        self,
        key: str,
        *,
        include_duration_aliases: bool = False,
        include_level_aliases: bool = False,
    ) -> list[str]:
        canonical = self._COMMAND_CANONICALS[key]
        names = [canonical, *self._command_aliases.get(key, [])]
        if include_duration_aliases and key == "video":
            names.extend(
                f"{name}{duration}"
                for name in names[:]
                for duration in range(100)
            )
        if include_level_aliases and key == "kkgifzip":
            names.extend(
                f"{name}{level}"
                for name in names[:]
                for level in range(1, 6)
            )
        if include_level_aliases and key in {"grokpack", "grokvg"}:
            zip_bases = [
                name
                for name in names[:]
                if name.casefold() in self._WORKFLOW_ZIP_BASES
                or name.casefold().endswith("z")
            ]
            names.extend(
                f"{name}{level}" for name in zip_bases for level in range(1, 6)
            )
        return list(dict.fromkeys(names))

    def _kkgifzip_command_names(self, *, include_levels: bool = True) -> list[str]:
        return self._command_names_for_key(
            "kkgifzip", include_level_aliases=include_levels
        )

    def _command_names_for_parser(self) -> list[str]:
        names: list[str] = ["kkgif"]
        for key in self._COMMAND_CANONICALS:
            names.extend(
                self._command_names_for_key(
                    key,
                    include_duration_aliases=key == "video",
                    include_level_aliases=key
                    in {"kkgifzip", "grokpack", "grokvg"},
                )
            )
        return list(dict.fromkeys(names))

    def _command_help_groups(self) -> dict[str, dict[str, object]]:
        return {
            "main": {
                "label": "主图像",
                "names": self._command_names_for_key("main"),
                "description": "文生图、修图、多图参考和引用图编辑",
            },
            "image2": {
                "label": "Image2",
                "names": self._command_names_for_key("image2"),
                "description": "独立通道；Images 模式最多一张参考图",
            },
            "grok": {
                "label": "Grok 生图",
                "names": self._command_names_for_key("grok"),
                "description": "Grok Images 文生图/图生图，支持多图参考",
            },
            "grok2": {
                "label": "Grok 2K",
                "names": self._command_names_for_key("grok2"),
                "description": "2K 文生图，不接受参考图",
            },
            "video": {
                "label": "Grok 视频",
                "names": self._command_names_for_key("video"),
                "description": "文生/图生视频；可在指令中写 1-15 秒",
            },
            "main_gif": {
                "label": "主通道 GIF 分镜",
                "names": self._command_names_for_key("main_gif"),
            },
            "main_gif2": {
                "label": "主通道 9 帧分镜",
                "names": self._command_names_for_key("main_gif2"),
            },
            "image2_gif": {
                "label": "Image2 GIF 分镜",
                "names": self._command_names_for_key("image2_gif"),
            },
            "image2_gif2": {
                "label": "Image2 9 帧分镜",
                "names": self._command_names_for_key("image2_gif2"),
            },
            "kkgifzip": {
                "label": "GIF 压缩",
                "names": self._command_names_for_key("kkgifzip"),
                "description": "本地压缩视频或 GIF；支持 1-5 档。",
            },
            "grokpack": {
                "label": "Grok 全套工作流",
                "names": self._command_names_for_key("grokpack"),
                "description": "图→视频→GIF；z/1-5 为压缩成品。",
            },
            "grokvg": {
                "label": "Grok 视频+GIF 工作流",
                "names": self._command_names_for_key("grokvg"),
                "description": "视频→GIF；z/1-5 为压缩成品。",
            },
            "admin_quota": {
                "label": "额度",
                "names": _DEFAULT_ADMIN_COMMAND_NAMES["quota"],
                "description": "管理员查询或设置三条通道日配额。",
            },
            "admin_reset": {
                "label": "重置额度",
                "names": _DEFAULT_ADMIN_COMMAND_NAMES["reset"],
                "description": "管理员清零今日已用次数，累计次数保留。",
            },
            "admin_moderation": {
                "label": "审核",
                "names": _DEFAULT_ADMIN_COMMAND_NAMES["moderation"],
                "description": "查询或切换本地敏感词审核。",
            },
            "help": {
                "label": "帮助",
                "names": self._help_command_names(),
                "description": "显示当前全部 canonical 指令和别名。",
            },
        }

    def _command_catalog(self) -> list[dict[str, object]]:
        """Return the same command/alias catalog used by help and the WebUI."""
        groups = self._command_help_groups()
        descriptions = {
            "main": "主图像文生图、修图、多图参考和引用图编辑。",
            "image2": "独立 Image2 通道；Images 模式最多一张参考图。",
            "grok": "Grok Images 文生图/图生图，支持多图参考。",
            "grok2": "Grok 2K 文生图，仅支持文字提示词。",
            "video": "Grok2API 异步文生/图生视频，支持 1-15 秒。",
            "main_gif": "主通道生成 4x4、16 帧 GIF 分镜。",
            "main_gif2": "主通道生成 3x3、9 帧 GIF 分镜。",
            "image2_gif": "Image2 通道生成 4x4、16 帧 GIF 分镜。",
            "image2_gif2": "Image2 通道生成 3x3、9 帧 GIF 分镜。",
            "kkgifzip": "本地压缩视频或 GIF 为更小动图；支持 1-5 档。",
            "grokpack": "Grok 全套：图→视频→GIF；过程合并转发，成品单独发。",
            "grokvg": "Grok 视频套：视频→GIF；过程合并转发，成品单独发。",
            "admin_quota": "管理员查询或设置三条通道日配额。",
            "admin_reset": "管理员清零今日已用次数，累计次数保留。",
            "admin_moderation": "查询或切换本地敏感词审核。",
            "help": "显示当前全部 canonical 指令和别名。",
        }
        catalog: list[dict[str, object]] = []
        for key, item in groups.items():
            names = [str(name) for name in item.get("names", [])]
            if not names:
                continue
            catalog.append(
                {
                    "key": key,
                    "primary": names[0],
                    "aliases": names[1:],
                    "names": names,
                    "description": descriptions.get(key, ""),
                }
            )
        catalog.append(
            {
                "key": "kkgif",
                "primary": "kkgif",
                "aliases": [],
                "names": ["kkgif"],
                "description": "把一个附带或引用的视频在本地转换为 GIF。",
            }
        )
        return catalog

    def _apply_configured_command_aliases(self) -> None:
        """Inject plugin aliases into AstrBot's registered command filters."""
        try:
            from astrbot.core.star.filter.command import CommandFilter
            from astrbot.core.star.star_handler import star_handlers_registry
        except ImportError:
            return

        wanted = {
            self._COMMAND_HANDLER_NAMES[key]: key
            for key in self._COMMAND_HANDLER_NAMES
        }
        for metadata in star_handlers_registry:
            if getattr(metadata, "handler_name", "") == self._HELP_HANDLER_NAME:
                for event_filter in getattr(metadata, "event_filters", []):
                    if not isinstance(event_filter, CommandFilter):
                        continue
                    if event_filter.command_name != "kkt帮助":
                        continue
                    help_names = self._help_command_names()
                    event_filter.alias = set(help_names[1:])
                    event_filter._cmpl_cmd_names = None
                    break
                continue
            key = wanted.get(getattr(metadata, "handler_name", ""))
            if key is None:
                continue
            canonical = self._COMMAND_CANONICALS[key]
            aliases = self._command_names_for_key(
                key,
                include_duration_aliases=key == "video",
                include_level_aliases=key
                in {"kkgifzip", "grokpack", "grokvg"},
            )[1:]
            for event_filter in getattr(metadata, "event_filters", []):
                if not isinstance(event_filter, CommandFilter):
                    continue
                if event_filter.command_name != canonical:
                    continue
                event_filter.alias = set(aliases)
                event_filter._cmpl_cmd_names = None
                break

    def _help_command_names(self) -> list[str]:
        """Return the help command plus each configured alias with help suffix."""
        names = ["kkt帮助", *_DEFAULT_HELP_ALIASES]
        for key in self._COMMAND_CANONICALS:
            for command_name in self._command_names_for_key(key):
                names.extend((f"{command_name}帮助", f"{command_name}help"))
        return list(dict.fromkeys(names))

    @classmethod
    def _parse_grokv_duration(
        cls,
        event: AstrMessageEvent,
        prompt: str,
        default_duration: int,
        command_names: list[str] | None = None,
    ) -> tuple[int, str, str | None]:
        """Parse ``/grokvideo 5`` and compact duration aliases."""
        text = (prompt or "").strip()
        raw = (event.get_message_str() or "").strip()
        duration: int | None = None

        names = command_names or ["grokvideo", "grokv", "gkv", "gv"]
        name_pattern = "|".join(
            re.escape(name) for name in sorted(set(names), key=len, reverse=True)
        )
        compact = re.match(
            rf"^/?(?:{name_pattern})(\d+)(?:\s|$)", raw, re.IGNORECASE
        )
        if compact:
            duration = int(compact.group(1))
        else:
            spaced = re.match(r"^(\d+)(?:\s+|$)(.*)$", text, re.DOTALL)
            if spaced:
                duration = int(spaced.group(1))
                text = spaced.group(2).strip()

        if duration is None:
            duration = default_duration
        if duration < 1 or duration > 15:
            return duration, text, "视频时长只能是 1-15 秒，例如 /grokvideo 5。"
        return duration, text, None

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
        """群聊可见：仅一行开/关，不暴露词条与类别。"""
        return f"本地审核：{'开' if self.sensitive_filter_enabled else '关'}"

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

    def _make_video_component(self, video_path: str):
        """与 link_resolver 一致：绝对路径 + Video.fromFileSystem。"""
        abs_path = str(Path(video_path).resolve())
        if not Path(abs_path).is_file() or Path(abs_path).stat().st_size <= 0:
            raise FileNotFoundError(f"视频文件无效: {abs_path}")
        return Comp.Video.fromFileSystem(abs_path)

    def _video_aspect_ratio_for_image(self, image_item: dict) -> str:
        """Pick the supported ratio closest to the reference image."""
        configured = self.video_aspect_ratio
        data_url = str(image_item.get("data_url") or "").strip()
        if not data_url.startswith("data:") or "," not in data_url:
            return configured
        try:
            _, encoded = data_url.split(",", 1)
            with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                width, height = image.size
            source_ratio = width / height
            candidates = {
                "1:1": 1.0,
                "16:9": 16 / 9,
                "9:16": 9 / 16,
                "4:3": 4 / 3,
                "3:4": 3 / 4,
                "3:2": 3 / 2,
                "2:3": 2 / 3,
            }
            selected = min(candidates, key=lambda ratio: abs(candidates[ratio] - source_ratio))
            logger.info(
                "[kkt] video aspect matched reference: image=%sx%s ratio=%.4f configured=%s selected=%s",
                width, height, source_ratio, configured, selected,
            )
            return selected
        except Exception as exc:
            logger.warning("[kkt] video aspect match failed, use configured=%s err=%s", configured, exc)
            return configured

    @staticmethod
    def _animated_reference_notice(image_items: list[dict]) -> str | None:
        frames = [
            str(item.get("animated_frame") or "").strip()
            for item in image_items
            if str(item.get("animated_frame") or "").strip()
        ]
        if not frames:
            return None
        unique = list(dict.fromkeys(frames))
        return f"参考动图已选取{'、'.join(unique)}。"

    @staticmethod
    def _safe_image_failure(exc: Exception) -> str:
        text = str(exc or "").lower()
        if "拒绝生成" in str(exc) or "safety" in text or "moderation" in text:
            return "图片生成被上游拒绝，请修改提示词后重试。"
        return "图片生成失败，请稍后重试。"

    @staticmethod
    def _safe_video_failure(exc: GrokVideoError) -> str:
        code = str(getattr(exc, "code", "") or "").lower()
        if code == "timeout":
            return "视频生成超时，请稍后重试。"
        if code in {"download_failed", "media_too_large", "invalid_response"}:
            return "视频已生成，但发送失败，请稍后重试。"
        if code in {"invalid_parameter", "invalid_request"}:
            return "视频参数无效，请检查提示词后重试。"
        return "视频生成失败，请稍后重试。"

    async def _send_video_direct(
        self,
        event: AstrMessageEvent,
        video_path: str,
        *,
        elapsed_seconds: int | None = None,
    ) -> None:
        """Direct Send Pattern（参考 link_resolver）：

        1) 单独 await event.send(MessageChain([Video]))，不走 yield/装饰链
        2) 视频发送成功后再发送完成文案
        3) 发送完成后再清理本地文件
        """
        video_component = self._make_video_component(video_path)
        logger.info(
            "[kkt] video direct send start: path=%s size=%d",
            video_path,
            Path(video_path).stat().st_size,
        )
        await event.send(MessageChain([video_component]))
        logger.info("[kkt] video direct send done: path=%s", video_path)

        tip_chain: list = []
        if self.reply_with_quote:
            message_id = self._extract_reaction_message_id(event)
            if message_id is not None:
                tip_chain.append(Comp.Reply(id=message_id))
        if elapsed_seconds is not None:
            tip_chain.append(
                Comp.Plain(f"视频生成成功，耗时：{elapsed_seconds}秒，请查收喵")
            )
        if tip_chain:
            try:
                await event.send(MessageChain(tip_chain))
            except Exception as exc:
                logger.warning("[kkt] video success tip send failed: %s", exc)

    def _check_video_cooldown(self, event: AstrMessageEvent) -> str | None:
        """视频通道独立 CD；管理员跳过。"""
        if self.video_cooldown_seconds <= 0:
            return None
        if self._is_admin(event):
            return None
        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            return None
        last = self._video_user_last_call.get(sender_id)
        if last is None:
            return None
        remain = self.video_cooldown_seconds - (time.monotonic() - last)
        if remain > 0:
            return f"视频操作太快了，请 {int(remain) + 1} 秒后再试。"
        return None

    def _mark_video_cooldown(self, event: AstrMessageEvent) -> None:
        if self.video_cooldown_seconds <= 0:
            return
        if self._is_admin(event):
            return
        sender_id = str(event.get_sender_id() or "").strip()
        if sender_id:
            self._video_user_last_call[sender_id] = time.monotonic()

    async def _try_acquire_video_slot(
        self, event: AstrMessageEvent
    ) -> tuple[bool, str | None]:
        """尝试占用视频并发槽（非阻塞）；失败返回 (False, 提示)。"""
        sender_id = str(event.get_sender_id() or "").strip() or "anonymous"
        async with self._video_slot_lock:
            user_n = int(self._video_user_inflight.get(sender_id, 0))
            if user_n >= self.video_max_concurrent_per_user:
                logger.info(
                    "[kkt] video per-user concurrent full: sender=%s inflight=%d limit=%d",
                    sender_id,
                    user_n,
                    self.video_max_concurrent_per_user,
                )
                return False, (
                    f"你已有视频在生成中（每用户最多 "
                    f"{self.video_max_concurrent_per_user} 个），请稍后再试。"
                )
            if self._video_global_inflight >= self.video_max_concurrent:
                logger.info(
                    "[kkt] video global concurrent full: inflight=%d limit=%d sender=%s",
                    self._video_global_inflight,
                    self.video_max_concurrent,
                    sender_id,
                )
                return False, (
                    f"当前视频生成队列已满（最多同时 {self.video_max_concurrent} 个），"
                    "请稍后再试。"
                )
            self._video_global_inflight += 1
            self._video_user_inflight[sender_id] = user_n + 1
            logger.info(
                "[kkt] video slot acquired: sender=%s user_inflight=%d global_inflight=%d",
                sender_id,
                self._video_user_inflight[sender_id],
                self._video_global_inflight,
            )
            return True, None

    async def _release_video_slot(self, event: AstrMessageEvent) -> None:
        sender_id = str(event.get_sender_id() or "").strip() or "anonymous"
        async with self._video_slot_lock:
            user_n = int(self._video_user_inflight.get(sender_id, 0))
            if user_n <= 1:
                self._video_user_inflight.pop(sender_id, None)
            else:
                self._video_user_inflight[sender_id] = user_n - 1
            self._video_global_inflight = max(0, self._video_global_inflight - 1)
            logger.info(
                "[kkt] video slot released: sender=%s user_inflight=%s global_inflight=%d",
                sender_id,
                self._video_user_inflight.get(sender_id, 0),
                self._video_global_inflight,
            )

    def _schedule_video_cleanup(self, path: str) -> None:
        delay = max(5, int(self.video_cleanup_delay))

        async def cleanup():
            await asyncio.sleep(delay)
            try:
                Path(path).unlink(missing_ok=True)
                logger.debug("[kkt] video temp cleaned: %s", path)
            except OSError as exc:
                logger.warning("[kkt] video temp cleanup failed: %s err=%s", path, exc)

        asyncio.create_task(cleanup())

    async def _materialize_video_bytes(
        self, content: bytes, content_type: str = "video/mp4"
    ) -> str:
        suffix = ".mp4"
        ctype = (content_type or "").lower()
        if "webm" in ctype:
            suffix = ".webm"
        elif "quicktime" in ctype or "mov" in ctype:
            suffix = ".mov"
        path = self.temp_dir / f"kkt_video_{int(time.time() * 1000)}{suffix}"
        path.write_bytes(content)
        logger.info(
            "[kkt] video saved: path=%s bytes=%d ctype=%s",
            path,
            len(content),
            content_type,
        )
        return str(path)

    async def _transcode_video_for_qq(self, src_path: str) -> str:
        """Re-mux/re-encode for NapCat/QQ: H.264 Main + AAC + faststart.

        Upstream grok mp4 often triggers FFmpeg 'timescale not set' and may
        silently fail to display in QQ even though NapCat logs send success.
        """
        src = Path(src_path)
        if not src.is_file():
            return src_path
        out = src.with_name(f"{src.stem}_qq.mp4")
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-c:v",
            "libx264",
            "-profile:v",
            "main",
            "-level",
            "3.1",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-r",
            "24",
            "-g",
            "48",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            "-brand",
            "mp42",
            str(out),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode != 0 or not out.is_file() or out.stat().st_size < 1024:
                err = (stderr or b"").decode("utf-8", errors="replace")[:300]
                logger.warning(
                    "[kkt] video transcode failed, use original: code=%s err=%s",
                    proc.returncode,
                    err,
                )
                return src_path
            logger.info(
                "[kkt] video transcoded for QQ: src=%s(%d) -> out=%s(%d)",
                src.name,
                src.stat().st_size,
                out.name,
                out.stat().st_size,
            )
            return str(out)
        except FileNotFoundError:
            logger.warning("[kkt] ffmpeg not found, skip video transcode")
            return src_path
        except Exception as exc:
            logger.warning("[kkt] video transcode error, use original: %s", exc)
            return src_path

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

    @classmethod
    def _channel_for_command(cls, command: str) -> str:
        """指令名 → 计费通道。"""
        name = str(command or "").strip().lower()
        if name in {cls._CHANNEL_IMAGE2, "image2gif", "image2gif2"}:
            return cls._CHANNEL_IMAGE2
        if name in {cls._CHANNEL_VIDEO, "grokvideo", "grokv"}:
            return cls._CHANNEL_VIDEO
        return cls._CHANNEL_MAIN

    def _cost_usd_for_channel(self, channel: str) -> float:
        if channel == self._CHANNEL_IMAGE2:
            return float(getattr(self, "cost_image2_usd", 0) or 0)
        if channel == self._CHANNEL_VIDEO:
            return float(getattr(self, "cost_video_usd", 0) or 0)
        return float(getattr(self, "cost_main_usd", 0) or 0)

    @staticmethod
    def _empty_channel_bucket() -> dict:
        return {"daily": 0, "total": 0}

    def _empty_usage_state(self) -> dict:
        today = date.today().isoformat()
        return {
            "date": today,
            "channels": {
                ch: self._empty_channel_bucket() for ch in self._CHANNELS
            },
        }

    def _normalize_usage_state(self, data: dict | None) -> dict:
        """规范化 usage；跨日则 daily 归零、total 保留。"""
        today = date.today().isoformat()
        state = self._empty_usage_state()
        if not isinstance(data, dict):
            return state
        channels_in = data.get("channels")
        if not isinstance(channels_in, dict):
            channels_in = {}
        same_day = str(data.get("date") or "") == today
        for ch in self._CHANNELS:
            raw = channels_in.get(ch)
            if not isinstance(raw, dict):
                raw = {}
            total = max(0, int(raw.get("total") or 0))
            daily = max(0, int(raw.get("daily") or 0)) if same_day else 0
            state["channels"][ch] = {"daily": daily, "total": total}
        # 兼容旧单桶：{"date","count"} 无 channels
        if "channels" not in data and "count" in data:
            legacy = max(0, int(data.get("count") or 0))
            if same_day or str(data.get("date") or "") == today:
                state["channels"][self._CHANNEL_MAIN]["daily"] = legacy
            # 旧 count 无法可靠推断 total；仅迁移当日 daily
        state["date"] = today
        return state

    def _load_usage_state(self) -> dict:
        """读取分通道用量（含旧 daily_quota.json 迁移）。"""
        try:
            if self.usage_path.exists():
                data = json.loads(self.usage_path.read_text(encoding="utf-8"))
                return self._normalize_usage_state(
                    data if isinstance(data, dict) else None
                )
            # 迁移旧单桶
            if self.quota_path.exists():
                legacy = json.loads(self.quota_path.read_text(encoding="utf-8"))
                state = self._normalize_usage_state(
                    legacy if isinstance(legacy, dict) else None
                )
                self._save_usage_state(state)
                logger.info(
                    "[kkt] 已从 daily_quota.json 迁移用量到 usage.json: %s",
                    state,
                )
                return state
        except Exception as exc:
            logger.warning("[kkt] 读取用量失败: %s", exc)
        return self._empty_usage_state()

    def _save_usage_state(self, state: dict) -> None:
        try:
            normalized = self._normalize_usage_state(state)
            self.usage_path.parent.mkdir(parents=True, exist_ok=True)
            self.usage_path.write_text(
                json.dumps(normalized, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # 同步写旧文件，便于回滚/外部脚本
            main_daily = int(
                normalized["channels"][self._CHANNEL_MAIN].get("daily") or 0
            )
            self.quota_path.write_text(
                json.dumps(
                    {
                        "date": normalized["date"],
                        "count": main_daily,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("[kkt] 写入用量失败: %s", exc)

    @staticmethod
    def _task_timestamp() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _load_task_logs(self) -> list[dict]:
        try:
            if self.task_log_path.exists():
                data = json.loads(self.task_log_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    logs = [item for item in data if isinstance(item, dict)]
                    for item in logs:
                        if item.get("status") == "running":
                            item["status"] = "interrupted"
                            item["code"] = "plugin_reload"
                    return logs[-100:]
        except Exception as exc:
            logger.warning("[kkt] 读取任务日志失败: %s", exc)
        return []

    def _save_task_logs(self) -> None:
        try:
            payload = [
                {key: value for key, value in item.items() if not key.startswith("_")}
                for item in self._task_logs[-100:]
            ]
            self.task_log_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("[kkt] 写入任务日志失败: %s", exc)

    def _start_task_log(
        self, *, channel: str, command: str, prompt: str, model: str
    ) -> str:
        task_id = f"task_{int(time.time() * 1000)}_{len(self._task_logs) + 1}"
        item = {
            "task_id": task_id,
            "channel": channel,
            "command": command,
            "model": model,
            "prompt": str(prompt or "").strip()[:500],
            "status": "running",
            "progress": 0,
            "code": "submitted",
            "request_id": "",
            "started_at": self._task_timestamp(),
            "finished_at": "",
            "duration_seconds": None,
            "_started_epoch": time.time(),
        }
        self._task_logs.append(item)
        self._save_task_logs()
        logger.info(
            "[kkt] task started: task_id=%s channel=%s command=%s model=%s prompt_len=%d",
            task_id,
            channel,
            command,
            model,
            len(item["prompt"]),
        )
        return task_id

    def _update_task_log(
        self, task_id: str, *, progress: int | None = None, request_id: str = ""
    ) -> None:
        for item in reversed(self._task_logs):
            if item.get("task_id") != task_id:
                continue
            if progress is not None:
                item["progress"] = max(0, min(100, int(progress)))
            if request_id:
                item["request_id"] = str(request_id)
                item["code"] = str(request_id)
            self._save_task_logs()
            return

    def _finish_task_log(
        self,
        task_id: str,
        *,
        status: str,
        code: str,
        progress: int,
        request_id: str = "",
    ) -> None:
        for item in reversed(self._task_logs):
            if item.get("task_id") != task_id:
                continue
            item["status"] = status
            item["progress"] = max(0, min(100, int(progress)))
            item["code"] = str(code or "")[:120]
            if request_id:
                item["request_id"] = str(request_id)
            item["finished_at"] = self._task_timestamp()
            started = item.get("_started_epoch")
            if isinstance(started, (int, float)):
                item["duration_seconds"] = max(0, int(round(time.time() - started)))
            self._save_task_logs()
            logger.info(
                "[kkt] task finished: task_id=%s status=%s code=%s progress=%d",
                task_id,
                status,
                str(code)[:120],
                item["progress"],
            )
            return

    def _get_task_logs(self, limit: int = 50) -> list[dict]:
        result = []
        for item in reversed(self._task_logs[-max(1, min(100, limit)):]):
            result.append(
                {key: value for key, value in item.items() if not key.startswith("_")}
            )
        return result

    # 兼容旧测试/调用
    def _load_quota_state(self) -> dict:
        usage = self._load_usage_state()
        main = usage["channels"][self._CHANNEL_MAIN]
        return {
            "date": usage["date"],
            "count": int(main.get("daily") or 0),
        }

    def _save_quota_state(self, state: dict) -> None:
        usage = self._load_usage_state()
        count = max(0, int(state.get("count") or 0))
        usage["channels"][self._CHANNEL_MAIN]["daily"] = count
        usage["date"] = date.today().isoformat()
        self._save_usage_state(usage)

    def _load_channel_limits(self) -> dict[str, int]:
        """读取各通道日限额；优先 channel 覆盖 → 旧单桶覆盖 → 配置默认。"""
        limits = {
            ch: int(self._daily_quota_config_defaults.get(ch, 0))
            for ch in self._CHANNELS
        }
        try:
            path = self.channel_limit_override_path
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    raw_limits = data.get("limits")
                    if isinstance(raw_limits, dict):
                        for ch in self._CHANNELS:
                            if ch in raw_limits:
                                limits[ch] = max(0, int(raw_limits[ch]))
                    return limits
            # 旧单桶覆盖 → 两通道同值（兼容）
            if self.quota_limit_override_path.exists():
                data = json.loads(
                    self.quota_limit_override_path.read_text(encoding="utf-8")
                )
                if isinstance(data, dict) and "limit" in data:
                    legacy = max(0, int(data.get("limit")))
                    for ch in self._CHANNELS:
                        limits[ch] = legacy
        except Exception as exc:
            logger.warning("[kkt] 读取通道限额失败，回退配置默认: %s", exc)
        return limits

    def _save_channel_limits(self, limits: dict[str, int]) -> None:
        payload = {
            "limits": {
                ch: max(0, int(limits.get(ch, 0))) for ch in self._CHANNELS
            },
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "command",
        }
        self.channel_limit_override_path.parent.mkdir(parents=True, exist_ok=True)
        self.channel_limit_override_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.channel_limits = {
            ch: max(0, int(limits.get(ch, 0))) for ch in self._CHANNELS
        }
        self.daily_quota = self.channel_limits[self._CHANNEL_MAIN]
        # 同步旧单桶覆盖文件（main）
        try:
            self.quota_limit_override_path.write_text(
                json.dumps(
                    {
                        "limit": self.channel_limits[self._CHANNEL_MAIN],
                        "updated_at": payload["updated_at"],
                        "source": "command",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("[kkt] 同步旧限额文件失败: %s", exc)
        logger.info("[kkt] 通道日限额已更新: %s", self.channel_limits)

    def _load_daily_quota_limit(self) -> int:
        """兼容：返回 main 通道日限额。"""
        return int(self._load_channel_limits().get(self._CHANNEL_MAIN, 0))

    def _save_daily_quota_limit(self, limit: int) -> None:
        """兼容：全部通道设为同一上限。"""
        limit = max(0, int(limit))
        self._save_channel_limits({ch: limit for ch in self._CHANNELS})

    @classmethod
    def _parse_channel_token(cls, text: str) -> str | None:
        """解析通道名；无法识别返回 None。"""
        raw = (text or "").strip().lower()
        if not raw:
            return None
        if raw in {"main", "kkt", "hajimi", "默认", "主通道"}:
            return cls._CHANNEL_MAIN
        if raw in {"image2", "img2", "image", "图2"}:
            return cls._CHANNEL_IMAGE2
        if raw in {"video", "grokv", "vid", "视频"}:
            return cls._CHANNEL_VIDEO
        if raw in {"all", "全部", "所有"}:
            return "all"
        return None

    @staticmethod
    def _parse_quota_limit_arg(text: str) -> int | None:
        """解析额度指令中的数字上限。

        支持：
        - 空 / 无数字 -> None（表示查询）
        - "10" / "额度 10" / "main 10" / "image2=20"
        """
        raw = (text or "").strip()
        if not raw:
            return None
        cleaned = re.sub(
            r"(?i)^\s*(?:额度|限额|配额|quota|limit|set|to|为|到|=|:|：|"
            r"main|kkt|hajimi|image2|img2|image|video|grokv|vid|"
            r"默认|主通道|图2|视频|全部|所有|all)+",
            "",
            raw,
        ).strip()
        cleaned = re.sub(r"\s+", "", cleaned)
        if not cleaned:
            return None
        if re.fullmatch(r"\d+", cleaned):
            return int(cleaned)
        match = re.search(r"(\d+)", raw)
        if match and re.fullmatch(r"[\D]*\d+[\D]*", raw.replace(" ", "")):
            return int(match.group(1))
        return None

    def _parse_quota_command_arg(
        self, text: str
    ) -> tuple[str | None, int | None, bool]:
        """解析额度参数 → (channel|None|all, limit|None, ok)。

        - 空：查询全部
        - main / image2：查询该通道
        - 10：两通道同设 10
        - main 10 / image2=20：设单通道
        ok=False 表示参数非法。
        """
        raw = (text or "").strip()
        if not raw:
            return None, None, True
        parts = raw.split()
        # 单 token
        if len(parts) == 1:
            token = parts[0]
            ch = self._parse_channel_token(token)
            if ch is not None:
                return ch, None, True
            # image2=10 / main:20
            m = re.fullmatch(
                r"(?i)(main|kkt|hajimi|image2|img2|image|video|grokv|vid|"
                r"默认|主通道|图2|视频)"
                r"\s*(?:=|：|:)?\s*(\d+)",
                token,
            )
            if m:
                return self._parse_channel_token(m.group(1)), int(m.group(2)), True
            limit = self._parse_quota_limit_arg(token)
            if limit is not None:
                return "all", limit, True
            return None, None, False
        # 多 token：通道 + 数字，或 词 + 数字
        ch = self._parse_channel_token(parts[0])
        rest = " ".join(parts[1:])
        limit = self._parse_quota_limit_arg(rest)
        if ch is not None and limit is not None:
            return ch, limit, True
        if ch is not None and not rest.strip():
            return ch, None, True
        # 无通道前缀：整段当数字
        limit2 = self._parse_quota_limit_arg(raw)
        if limit2 is not None:
            return "all", limit2, True
        return None, None, False

    @staticmethod
    def _format_usd(amount: float) -> str:
        if amount <= 0:
            return "$0"
        if amount < 0.01:
            return f"${amount:.4f}"
        text = f"{amount:.4f}".rstrip("0").rstrip(".")
        return f"${text}"

    def _format_channel_usage_block(self, channel: str, usage: dict) -> str:
        label = self._CHANNEL_LABELS.get(channel, channel)
        bucket = usage["channels"].get(channel) or self._empty_channel_bucket()
        daily = int(bucket.get("daily") or 0)
        total = int(bucket.get("total") or 0)
        limit = int(self.channel_limits.get(channel, 0))
        unit = self._cost_usd_for_channel(channel)
        cost_total = total * unit
        cost_daily = daily * unit
        if limit <= 0:
            daily_part = f"今日 {daily}（不限制）"
        else:
            remain = max(0, limit - daily)
            daily_part = f"今日 {daily}/{limit}（剩余 {remain}）"
        line1 = f"【{label}】{daily_part} · 累计 {total} 次"
        line2 = (
            f"  单价 {self._format_usd(unit)}/次 · "
            f"今日 {self._format_usd(cost_daily)} · "
            f"累计 {self._format_usd(cost_total)}"
        )
        return f"{line1}\n{line2}"

    def _format_quota_status(self, event: AstrMessageEvent | None = None) -> str:
        """生成分通道限额/用量/费用文案。"""
        usage = self._load_usage_state()
        lines = [
            self._format_channel_usage_block(ch, usage) for ch in self._CHANNELS
        ]
        total_all = sum(
            int((usage["channels"][ch].get("total") or 0)) for ch in self._CHANNELS
        )
        daily_all = sum(
            int((usage["channels"][ch].get("daily") or 0)) for ch in self._CHANNELS
        )
        cost_all = sum(
            int((usage["channels"][ch].get("total") or 0))
            * self._cost_usd_for_channel(ch)
            for ch in self._CHANNELS
        )
        cost_daily_all = sum(
            int((usage["channels"][ch].get("daily") or 0))
            * self._cost_usd_for_channel(ch)
            for ch in self._CHANNELS
        )
        lines.append(
            f"【合计】今日 {daily_all} 次 / 累计 {total_all} 次 · "
            f"今日 {self._format_usd(cost_daily_all)} / "
            f"累计 {self._format_usd(cost_all)}"
        )

        if self.cooldown_seconds <= 0:
            cd_line = "出图冷却：关闭"
        elif event is not None and self._is_admin(event):
            cd_line = f"出图冷却：{self.cooldown_seconds}s（管理员免冷却）"
        elif event is not None:
            sender_id = str(event.get_sender_id() or "").strip()
            last = self._user_last_call.get(sender_id) if sender_id else None
            if last is None:
                cd_line = f"出图冷却：{self.cooldown_seconds}s"
            else:
                remain_cd = self.cooldown_seconds - (time.monotonic() - last)
                if remain_cd > 0:
                    cd_line = f"出图冷却：还需 {int(remain_cd) + 1}s"
                else:
                    cd_line = f"出图冷却：{self.cooldown_seconds}s"
        else:
            cd_line = f"出图冷却：{self.cooldown_seconds}s"
        lines.append(cd_line)
        video_cd = int(getattr(self, "video_cooldown_seconds", 0) or 0)
        video_max = int(getattr(self, "video_max_concurrent", 2) or 2)
        video_per = int(getattr(self, "video_max_concurrent_per_user", 1) or 1)
        if video_cd <= 0:
            lines.append("视频冷却：关闭")
        else:
            lines.append(
                f"视频冷却：{video_cd}s · "
                f"并发上限 {video_max}"
                f"（每用户 {video_per}）"
            )
        return "\n".join(lines)

    async def _set_channel_quota_limit(
        self, channel: str, limit: int
    ) -> dict:
        """设置单通道或全部通道日限额；不改已用。"""
        limit = max(0, int(limit))
        async with self._quota_lock:
            new_limits = dict(self.channel_limits)
            if channel == "all":
                for ch in self._CHANNELS:
                    new_limits[ch] = limit
            else:
                ch = channel if channel in self._CHANNELS else self._CHANNEL_MAIN
                new_limits[ch] = limit
            self._save_channel_limits(new_limits)
            usage = self._load_usage_state()
            return {
                "limits": dict(self.channel_limits),
                "usage": usage,
            }

    async def _set_daily_quota_limit(self, limit: int) -> dict:
        """兼容：两通道同设。"""
        result = await self._set_channel_quota_limit("all", limit)
        usage = result["usage"]
        main_daily = int(
            usage["channels"][self._CHANNEL_MAIN].get("daily") or 0
        )
        return {
            "limit": self.channel_limits[self._CHANNEL_MAIN],
            "used": main_daily,
            "date": usage.get("date") or date.today().isoformat(),
        }

    async def _reset_channel_quota(self, channel: str = "all") -> dict:
        """清零今日 daily（total 保留）。channel=all|main|image2。"""
        async with self._quota_lock:
            usage = self._load_usage_state()
            targets = (
                list(self._CHANNELS)
                if channel == "all"
                else [channel if channel in self._CHANNELS else self._CHANNEL_MAIN]
            )
            for ch in targets:
                usage["channels"][ch]["daily"] = 0
            usage["date"] = date.today().isoformat()
            self._save_usage_state(usage)
            logger.info("[kkt] 日配额已重置: channel=%s state=%s", channel, usage)
            return usage

    async def _reset_daily_quota(self) -> dict:
        """兼容：清零全部通道今日已用。"""
        usage = await self._reset_channel_quota("all")
        return {
            "date": usage["date"],
            "count": 0,
        }

    async def _check_channel_quota(
        self, event: AstrMessageEvent, channel: str
    ) -> str | None:
        """仅检查通道日限额，不扣次。超限非管理员返回提示。"""
        limit = int(self.channel_limits.get(channel, 0))
        if limit <= 0:
            return None
        async with self._quota_lock:
            usage = self._load_usage_state()
            used = int(usage["channels"][channel].get("daily") or 0)
            if used >= limit:
                if self._is_admin(event):
                    logger.info(
                        "[kkt] 通道日配额已满但仍允许管理员: channel=%s used=%d limit=%d",
                        channel,
                        used,
                        limit,
                    )
                    return None
                label = self._CHANNEL_LABELS.get(channel, channel)
                return f"今日 {label} 额度已用完（{used}/{limit}）"
            return None

    async def _record_successful_usage(self, channel: str) -> None:
        """出图成功后 +1 daily 与 total（持久化）。"""
        if channel not in self._CHANNELS:
            channel = self._CHANNEL_MAIN
        async with self._quota_lock:
            usage = self._load_usage_state()
            bucket = usage["channels"][channel]
            bucket["daily"] = int(bucket.get("daily") or 0) + 1
            bucket["total"] = int(bucket.get("total") or 0) + 1
            usage["date"] = date.today().isoformat()
            self._save_usage_state(usage)
            unit = self._cost_usd_for_channel(channel)
            logger.info(
                "[kkt] 用量记账: channel=%s daily=%d total=%d unit_usd=%.4f est=%.4f",
                channel,
                bucket["daily"],
                bucket["total"],
                unit,
                bucket["total"] * unit,
            )

    async def _check_and_consume_daily_quota(
        self, event: AstrMessageEvent, channel: str | None = None
    ) -> str | None:
        """兼容旧接口。

        - 传入 channel：仅检查该通道日限（成功记账用 _record_successful_usage）
        - 不传 channel：按 main/daily_quota 预扣（兼容旧单测）
        """
        if channel is not None:
            return await self._check_channel_quota(event, channel)

        limit = 0
        if getattr(self, "channel_limits", None):
            limit = int(self.channel_limits.get(self._CHANNEL_MAIN, 0))
        if getattr(self, "daily_quota", None) is not None:
            limit = int(self.daily_quota)
        if limit <= 0:
            return None

        async with self._quota_lock:
            # 完整路径
            if getattr(self, "usage_path", None) is not None:
                usage = self._load_usage_state()
                used = int(
                    usage["channels"][self._CHANNEL_MAIN].get("daily") or 0
                )
                if used >= limit:
                    if self._is_admin(event):
                        return None
                    return f"今日额度已用完（{used}/{limit}）"
                usage["channels"][self._CHANNEL_MAIN]["daily"] = used + 1
                usage["channels"][self._CHANNEL_MAIN]["total"] = (
                    int(usage["channels"][self._CHANNEL_MAIN].get("total") or 0)
                    + 1
                )
                usage["date"] = date.today().isoformat()
                self._save_usage_state(usage)
                return None

            # 极简路径（单测只挂 quota_path）
            today = date.today().isoformat()
            state = {"date": today, "count": 0}
            path = getattr(self, "quota_path", None)
            if path is not None and Path(path).exists():
                try:
                    data = json.loads(Path(path).read_text(encoding="utf-8"))
                    if isinstance(data, dict) and str(data.get("date") or "") == today:
                        state["count"] = max(0, int(data.get("count") or 0))
                except Exception:
                    pass
            used = int(state.get("count") or 0)
            if used >= limit:
                if self._is_admin(event):
                    return None
                return f"今日额度已用完（{used}/{limit}）"
            state["count"] = used + 1
            if path is not None:
                try:
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    Path(path).write_text(
                        json.dumps(
                            state, ensure_ascii=False, separators=(",", ":")
                        ),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
            return None

    @classmethod
    def _command_arg_from_text(
        cls, text: str, command_names: list[str] | None = None
    ) -> str | None:
        """从完整指令文本中截取命令后的参数，支持运行时别名。"""
        text = (text or "").strip()
        if not text:
            return None
        if command_names is None:
            match = cls._CMD_ARG_RE.match(text)
        else:
            names = sorted(
                {str(name).strip() for name in command_names if str(name).strip()},
                key=len,
                reverse=True,
            )
            if not names:
                return None
            name_pattern = "|".join(re.escape(name) for name in names)
            pattern = re.compile(
                rf"^/?(?:{name_pattern})(?:帮助|help|\?)?(?:\s+|$)(.*)$",
                re.IGNORECASE | re.DOTALL,
            )
            match = pattern.match(text)
        if not match:
            return None
        return match.group(1).strip()

    @staticmethod
    def _is_gif_command(command: str) -> bool:
        return str(command or "").strip().lower() in {
            "hajimigif",
            "hajimigif2",
            "kktgif",
            "image2gif",
            "image2gif2",
        }

    @staticmethod
    def _gif_grid_size(command: str) -> int:
        """Return the square grid dimension for a GIF command."""
        return 3 if str(command or "").strip().lower().endswith("gif2") else 4

    @staticmethod
    def _gif_api_command(command: str) -> str:
        """Map GIF variants to the underlying image API channel."""
        return "image2" if str(command or "").strip().lower().startswith("image2") else "hajimi"

    def _build_gif_prompt(self, prompt: str, grid_size: int = 4) -> str:
        """Turn a user action into a fixed-layout 16-frame storyboard request."""
        frame_count = grid_size * grid_size
        action = (prompt or "").strip()
        if action:
            action_instruction = (
                f"用户指定的效果：{action}\n"
                "请先理解这个效果，再把它设计成一个简单、清晰、适合聊天表情包的循环动作。"
            )
        else:
            action_instruction = (
                "用户没有指定具体动作。请你自行选择一个适合参考主体的简单、可爱、"
                "容易看懂且适合循环播放的动作，例如轻微挥手、点头、眨眼、摇摆或卖萌；"
                "不要加入复杂场景、快速镜头运动或需要额外角色的剧情。"
            )
        background = (
            "如果用户要求抠图、去背景或贴纸效果，使用统一纯色背景；"
            "不要在不同格子中改变背景颜色。"
            if re.search(r"抠|去背|透明|贴纸|表情包", action, re.IGNORECASE)
            else "保持统一、简单的背景，不改变场景布局。"
        )
        return (
            "你正在生成一张供程序裁切成聊天表情包 GIF 的动作分镜图。\n"
            "【固定画布与对齐要求】\n"
            f"整张图必须是正方形，并严格覆盖完整的 {grid_size} 列 {grid_size} 行区域，共 {frame_count} 个等大画格。"
            "画格按照从左到右、从上到下的顺序排列，不能多格、少格、合并、重叠或改变行列。"
            "不要绘制可见格线、边框、序号或文字；每个画格占据整张图对应的等分区域。"
            "这是给程序按固定坐标裁切的图，不要把 16 格画成一张连续的大画面。\n"
            "【连续动画与固定镜头】\n"
            f"把 {frame_count} 个画格理解为同一段动作在连续时间中的等间隔采样，而不是互不相关的姿势设定图。"
            "相邻画格之间只发生小幅、渐进、可预测的变化；不要从一个姿势突然跳到另一个姿势。"
            "动作要有自然的缓入、加速、缓出和回弹，避免相邻画格出现明显跳帧。"
            "全程锁定同一台相机：固定机位、固定焦距、固定景别、固定视角、固定主体位置。"
            "禁止推近、拉远、横移、摇镜、旋转镜头、切换视角、切换景别或改变背景透视。\n"
            "【主体与安全边距】\n"
            "如果有参考图，不要擅自认定只有一个主角：自然保留其中重要的人物、动物、物体和环境。"
            "参考图中有多个人或多个主体时，默认让他们共同自然发展和互动；只有用户明确指定某个对象时，才突出该对象。"
            "参考图中没有人物时，不要强行添加或提取人物，让动物、物体、场景或抽象主体自然成为动画对象。"
            "每个画格中的重要主体都必须完整位于当前画格内部，并在四周保留明显安全边距。"
            "身体、四肢、头发、尾巴、道具和特效都不得跨越画格边界。"
            "保持镜头角度、景别、画风、光照和构图一致，只改变动作和表情。\n"
            "【用户想要的效果】\n"
            f"{action_instruction}\n"
            f"请将动作按连续时间展开到 {frame_count} 个画格，每格是前一格的自然延续，不要只列出离散关键姿势。"
            f"第 1 格作为动作起点，第 {frame_count} 格回到接近第 1 格的姿势；首尾之间也要有平滑过渡，方便循环播放。"
            "动作可以夸张，但不能超出当前画格；复杂动作要简化为聊天表情包能看懂的连续变化。\n"
            f"【背景与输出】\n{background}\n"
            "不要标题、序号、对白、气泡、水印、额外人物或装饰元素。"
            f"输出规则、整齐、适合程序固定等分裁切的 {grid_size}x{grid_size} 动作分镜图。"
        )

    def _make_gif_from_grid(
        self, source_path: str, grid_size: int = 4
    ) -> tuple[str, int, int]:
        """Crop a square storyboard and encode a compact GIF."""
        source = Path(source_path)
        with Image.open(source) as opened:
            image = opened.convert("RGB")
            side = min(image.width, image.height)
            if side < grid_size * 128:
                raise RuntimeError(
                    f"分镜图分辨率过低：{image.width}x{image.height}，每格至少需要 128 像素"
                )
            left = (image.width - side) // 2
            top = (image.height - side) // 2
            square = image.crop((left, top, left + side, top + side))
            cell = side // grid_size
            if cell < 128:
                raise RuntimeError("分镜图裁切后单帧尺寸过低")

            frames = []
            for row in range(grid_size):
                for column in range(grid_size):
                    frame = square.crop(
                        (column * cell, row * cell, (column + 1) * cell, (row + 1) * cell)
                    )
                    frame = frame.resize(
                        (self.gif_frame_size, self.gif_frame_size), Image.Resampling.LANCZOS
                    )
                    frames.append(frame)

        gif_path = self.temp_dir / f"kkt_gif_{int(time.time() * 1000)}.gif"
        frames[0].save(
            gif_path,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=max(20, round(1000 / self.gif_fps)),
            loop=0,
            optimize=True,
            disposal=2,
        )
        size = gif_path.stat().st_size
        if size > self.gif_max_bytes:
            gif_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"GIF 文件过大：{size / 1024 / 1024:.1f}MB，限制为 {self.gif_max_bytes / 1024 / 1024:.1f}MB"
            )
        return str(gif_path), len(frames), cell

    @classmethod
    def _strip_at_tokens(cls, text: str) -> str:
        """去掉 @昵称 / @昵称(QQ) 噪声，保留真实提示词。"""
        cleaned = cls._AT_TOKEN_RE.sub(" ", text or "")
        return re.sub(r"\s+", " ", cleaned).strip()

    @classmethod
    def _is_help_token(cls, text: str) -> bool:
        return (text or "").strip().lower() in {"帮助", "help", "?"}

    @classmethod
    def _extract_prompt(
        cls,
        event: AstrMessageEvent,
        prompt: str,
        command_names: list[str] | None = None,
    ) -> str:
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
        from_plain = cls._command_arg_from_text(plain_text, command_names)
        if from_plain is not None:
            candidates.append(from_plain)

        # 2) 整句 message_str（可能含 @昵称(QQ)）
        raw = (event.get_message_str() or "").strip()
        from_raw = cls._command_arg_from_text(raw, command_names)
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
        alias=set(_DEFAULT_ADMIN_COMMAND_NAMES["quota"][1:]),
    )
    async def handle_quota_status(self, event: AstrMessageEvent, arg: GreedyStr = ""):
        """查看或设置主图像、Image2、Grok 视频分通道日配额（含预估费用）。

        - /kkt额度 -> 查询（仅管理员）
        - /kkt额度 10 -> 全部通道日上限改为 10
        - /kkt额度 main 100 / /kkt额度 image2 20 / /kkt额度 video 5 -> 单通道
        """
        group_id = str(event.get_group_id() or "").strip()
        if group_id and group_id in self.group_blacklist:
            return
        event.stop_event()

        if not self._is_admin(event):
            yield event.plain_result("仅管理员可查询或调整额度。")
            return

        raw_arg = str(arg or "").strip()
        if not raw_arg:
            msg = (event.get_message_str() or "").strip()
            raw_arg = re.sub(
                r"(?i)^/?(?:kkt|hajimi|image2)(?:额度|限额|配额|quota|统计)\s*",
                "",
                msg,
            ).strip()

        channel, limit, ok = self._parse_quota_command_arg(raw_arg)
        if not ok:
            yield event.plain_result(
                "参数无效。\n"
                "查询：/kkt额度\n"
                "设置：/kkt额度 10 或 /kkt额度 main 100\n"
                "     /kkt额度 image2 20 或 /kkt额度 video 5"
            )
            return

        if limit is None:
            yield event.plain_result(self._format_quota_status(event))
            return

        target = channel or "all"
        old_limits = dict(self.channel_limits)
        await self._set_channel_quota_limit(target, limit)
        if target == "all":
            head = f"全通道日限额 → {limit}（0=不限制）"
        else:
            label = self._CHANNEL_LABELS.get(target, target)
            old_v = old_limits.get(target, 0)
            head = f"{label} 日限额 {old_v} → {limit}（0=不限制）"
        logger.info(
            "[kkt] 管理员调整通道限额: operator=%s target=%s limit=%d old=%s new=%s",
            event.get_sender_id(),
            target,
            limit,
            old_limits,
            self.channel_limits,
        )
        yield event.plain_result(head + "\n" + self._format_quota_status(event))

    @filter.command(
        "kkt重置额度",
        alias=set(_DEFAULT_ADMIN_COMMAND_NAMES["reset"][1:]),
    )
    async def handle_quota_reset(
        self, event: AstrMessageEvent, arg: GreedyStr = ""
    ):
        """重置今日已用次数（仅管理员）；累计 total 保留；不改日限额。支持 all、main、image2、video。"""
        group_id = str(event.get_group_id() or "").strip()
        if group_id and group_id in self.group_blacklist:
            return
        event.stop_event()
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可重置额度。")
            return
        raw_arg = str(arg or "").strip()
        if not raw_arg:
            msg = (event.get_message_str() or "").strip()
            raw_arg = re.sub(
                r"(?i)^/?(?:kkt|hajimi|image2)(?:重置额度|清零额度|resetquota)\s*",
                "",
                msg,
            ).strip()
        channel = self._parse_channel_token(raw_arg) if raw_arg else "all"
        if raw_arg and channel is None:
            yield event.plain_result(
                "参数无效。/kkt重置额度 [all|main|image2|video]"
            )
            return
        if channel is None:
            channel = "all"
        await self._reset_channel_quota(channel)
        label = "全部通道" if channel == "all" else self._CHANNEL_LABELS.get(
            channel, channel
        )
        text = f"已清零今日已用（{label}，累计次数保留）\n" + self._format_quota_status(
            event
        )
        logger.info(
            "[kkt] 管理员重置今日已用: operator=%s channel=%s limits=%s",
            event.get_sender_id(),
            channel,
            self.channel_limits,
        )
        yield event.plain_result(text)

    @filter.command(
        "kkt审核",
        alias=set(_DEFAULT_ADMIN_COMMAND_NAMES["moderation"][1:]),
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
            yield event.plain_result("仅管理员可开关本地审核。")
            return

        old = self.sensitive_filter_enabled
        self._save_sensitive_filter_enabled(toggle)
        logger.info(
            "[kkt] 管理员切换本地审核: operator=%s old=%s new=%s words=%d cats=%s",
            event.get_sender_id(),
            old,
            toggle,
            self._sensitive_word_count,
            sorted(self._sensitive_words_by_cat.keys()),
        )
        yield event.plain_result(self._format_sensitive_status())

    def _detect_command_name(self, event: AstrMessageEvent) -> str:
        """从消息中识别 canonical command，别名和视频紧凑时长均支持。"""

        def resolve_token(token: str) -> str | None:
            normalized = token.lstrip("/").casefold()
            mapping = getattr(self, "_command_alias_map", {})
            key = mapping.get(normalized)
            if key == "kkgif":
                return "kkgif"
            if key:
                return self._COMMAND_CANONICALS[key]

            # 兼容某些未经过 CommandFilter 的自定义视频别名+时长写法。
            for name in self._command_names_for_key("video"):
                if normalized.startswith(f"{name.casefold()}"):
                    suffix = normalized[len(name) :]
                    if suffix.isdigit():
                        return self._COMMAND_CANONICALS["video"]
            # 兼容自定义 kkgifzip 别名 + 档位：/gifz3
            for name in self._command_names_for_key("kkgifzip"):
                folded = name.casefold()
                if normalized == folded:
                    return "kkgifzip"
                if normalized.startswith(folded):
                    suffix = normalized[len(folded) :]
                    if suffix.isdigit() and 1 <= int(suffix) <= 5:
                        return "kkgifzip"
            # 工作流 z 档：/gkpz3 /gvgz5
            for key in ("grokpack", "grokvg"):
                for name in self._command_names_for_key(key):
                    folded = name.casefold()
                    if normalized == folded:
                        return key
                    if normalized.startswith(folded):
                        suffix = normalized[len(folded) :]
                        if suffix.isdigit() and 1 <= int(suffix) <= 5:
                            return key
            return None

        raw = (event.get_message_str() or "").strip()
        first = raw.split()[0] if raw.split() else ""
        resolved = resolve_token(first)
        if resolved:
            return resolved

        # 兜底：Plain 拼接，部分适配器的 message_str 不含 At/Plain 全文。
        plain = "".join(
            getattr(c, "text", "") or ""
            for c in event.get_messages()
            if isinstance(c, Comp.Plain)
        ).strip()
        resolved = resolve_token(plain.split()[0] if plain.split() else "")
        if resolved:
            return resolved
        return self._COMMAND_CANONICALS["main"]

    def _resolve_api_credentials(
        self, command: str
    ) -> tuple[str, list[str], str] | str:
        """返回 (api_base, api_keys[主+备], model)；失败返回错误文案。"""
        if command in {"grok", "grok2"}:
            keys = self._build_key_chain(
                getattr(self, "grok_api_key", "")
                or getattr(self, "api_key", ""),
                getattr(self, "grok_backup_api_keys", None)
                or getattr(self, "backup_api_keys", None),
            )
            if not keys:
                return (
                    "未配置 Grok 生图 API Key。请填写 grok_api_key；"
                    "留空时会复用主图像 api_key。"
                )
            base = (
                getattr(self, "grok_api_base", "")
                or getattr(self, "api_base", "")
            )
            return base, keys, self._GROK_IMAGE_MODEL
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
        model = self._GROK_IMAGE_MODEL if command == "grok" else self.model
        return self.api_base, keys, model

    @filter.command("kkgif")
    async def handle_kkgif(self, event: AstrMessageEvent, prompt: GreedyStr = ""):
        """把一个附带或引用的视频在本地转换为优化后的 GIF，不调用模型。"""
        group_id = str(event.get_group_id() or "").strip()
        if group_id and group_id in self.group_blacklist:
            logger.debug("[kkt] kkgif 忽略黑名单群: group_id=%s", group_id)
            return
        event.stop_event()
        prompt = self._extract_prompt(
            event, prompt, self._command_names_for_parser()
        )
        if self._is_help_token(prompt) or str(prompt or "").strip().lower() in {"help", "帮助", "?"}:
            yield event.plain_result(
                "康康视频转 GIF\n用法：引用或附带一个视频后发送 /kkgif\n"
                "限制：仅支持一个视频，最长 16 秒，输出不含声音。\n"
                "压缩请用 /kkgifzip 或 /gifz（可带 1-5 档）。"
            )
            return

        try:
            videos = await self._collect_videos(event)
        except Exception as exc:
            logger.exception("[kkt] kkgif 收集视频失败: %s", exc)
            yield event.plain_result("没有读取到有效视频，请直接附带或回复一个视频。")
            return
        if not videos:
            yield event.plain_result(
                "请附带或回复一个视频后再发送 /kkgif。\n"
                "限制：仅支持一个视频，最长 16 秒。"
            )
            return
        if len(videos) > 1:
            logger.info("[kkt] kkgif 多视频拦截: count=%d", len(videos))
            yield event.plain_result("每次只能转换一个视频为 GIF，请只保留一个视频。")
            return

        task_id = self._start_task_log(
            channel="gif", command="kkgif", prompt="", model="ffmpeg-gif"
        )
        source_path = ""
        output_path = ""
        try:
            source_path = await videos[0].convert_to_file_path()
            await event.send(
                event.plain_result("喵～正在把视频压成 GIF，马上就好啦。")
            )
            output_path, duration, dimension, fps = await self._convert_video_to_gif(
                source_path
            )
            self._finish_task_log(
                task_id,
                status="success",
                code="converted",
                progress=100,
            )
            logger.info(
                "[kkt] kkgif 转换成功: source=%s output=%s duration=%.2fs dimension=%d fps=%d",
                source_path,
                output_path,
                duration,
                dimension,
                fps,
            )
            try:
                image = Comp.Image.fromFileSystem(str(Path(output_path).resolve()))
            except Exception:
                image = Comp.Image(file=str(Path(output_path).resolve()), path=output_path)
            await event.send(MessageChain([image]))
            await event.send(event.plain_result("GIF 生成成功，喵～"))
        except Exception as exc:
            logger.exception("[kkt] kkgif 转换失败: %s", exc)
            code = "video_too_long" if "16 秒" in str(exc) else type(exc).__name__
            self._finish_task_log(task_id, status="failed", code=code, progress=0)
            if "16 秒" in str(exc):
                yield event.plain_result("视频超过 16 秒，无法转换为 GIF。")
            else:
                yield event.plain_result("GIF 转换失败，请确认视频格式和时长后重试。")
        finally:
            if output_path:
                try:
                    Path(output_path).unlink(missing_ok=True)
                except OSError:
                    pass

    @filter.command(
        "kkgifzip",
        alias={
            "gifz",
            "gifzip",
            "kkgifzip1",
            "kkgifzip2",
            "kkgifzip3",
            "kkgifzip4",
            "kkgifzip5",
            "gifz1",
            "gifz2",
            "gifz3",
            "gifz4",
            "gifz5",
            "gifzip1",
            "gifzip2",
            "gifzip3",
            "gifzip4",
            "gifzip5",
        },
    )
    async def handle_kkgifzip(self, event: AstrMessageEvent, prompt: GreedyStr = ""):
        """五档压缩视频或 GIF；静态图不支持。"""
        group_id = str(event.get_group_id() or "").strip()
        if group_id and group_id in self.group_blacklist:
            logger.debug("[kkt] kkgifzip 忽略黑名单群: group_id=%s", group_id)
            return
        event.stop_event()
        level = self._parse_kkgifzip_level(event)
        prompt = self._extract_prompt(
            event, prompt, self._command_names_for_parser()
        )
        if self._is_help_token(prompt) or str(prompt or "").strip().lower() in {
            "help",
            "帮助",
            "?",
        }:
            yield event.plain_result(self._kkgifzip_help_text())
            return

        try:
            videos = await self._collect_videos(event)
        except Exception as exc:
            logger.exception("[kkt] kkgifzip 收集视频失败: %s", exc)
            videos = []
        try:
            gifs = await self._collect_gif_images(event)
        except Exception as exc:
            logger.exception("[kkt] kkgifzip 收集 GIF 失败: %s", exc)
            gifs = []

        static_count = 0
        try:
            static_count = await self._count_static_images(event)
        except Exception as exc:
            logger.debug("[kkt] kkgifzip 静态图检测失败: %s", exc)

        if len(videos) > 1:
            yield event.plain_result("每次只能压缩一个视频，请只保留一个视频。")
            return
        if len(gifs) > 1:
            yield event.plain_result("每次只能压缩一个 GIF，请只保留一个动图。")
            return
        if videos and gifs:
            yield event.plain_result("请只保留一个视频或一个 GIF，不要混发。")
            return
        if not videos and not gifs:
            if static_count:
                yield event.plain_result(
                    f"不支持静态图。请附带或回复视频/GIF（当前 {level} 档）。"
                )
            else:
                yield event.plain_result(
                    f"请附带或回复一个视频或 GIF（当前 {level} 档）。"
                )
            return

        source_kind = "video" if videos else "gif"
        component = videos[0] if videos else gifs[0]
        task_id = self._start_task_log(
            channel="gif",
            command=f"kkgifzip{level}",
            prompt="",
            model="ffmpeg-gifzip",
        )
        source_path = ""
        output_path = ""
        try:
            source_path = await component.convert_to_file_path()
            await event.send(event.plain_result(f"压缩中，{level} 档…"))
            if source_kind == "video":
                output_path, duration, dimension, fps, colors = (
                    await self._convert_media_to_zip_gif(
                        source_path, level=level, source_kind="video"
                    )
                )
            else:
                if not await self._path_is_animated_gif(source_path):
                    self._finish_task_log(
                        task_id, status="failed", code="static_image", progress=0
                    )
                    yield event.plain_result(
                        "不支持静态图片。请附带或回复一个视频或 GIF。"
                    )
                    return
                output_path, duration, dimension, fps, colors = (
                    await self._convert_media_to_zip_gif(
                        source_path, level=level, source_kind="gif"
                    )
                )
            self._finish_task_log(
                task_id,
                status="success",
                code="converted",
                progress=100,
            )
            logger.info(
                "[kkt] kkgifzip 转换成功: level=%d kind=%s source=%s output=%s "
                "duration=%.2fs dimension=%d fps=%d colors=%d",
                level,
                source_kind,
                source_path,
                output_path,
                duration,
                dimension,
                fps,
                colors,
            )
            try:
                image = Comp.Image.fromFileSystem(str(Path(output_path).resolve()))
            except Exception:
                image = Comp.Image(
                    file=str(Path(output_path).resolve()), path=output_path
                )
            await event.send(MessageChain([image]))
            await event.send(event.plain_result(f"完成，{level} 档。"))
        except Exception as exc:
            logger.exception("[kkt] kkgifzip 转换失败: %s", exc)
            code = "video_too_long" if "16 秒" in str(exc) else type(exc).__name__
            self._finish_task_log(task_id, status="failed", code=code, progress=0)
            if "16 秒" in str(exc):
                yield event.plain_result("视频超过 16 秒，无法压缩为 GIF。")
            elif "静态" in str(exc):
                yield event.plain_result("不支持静态图片。请附带或回复一个视频或 GIF。")
            else:
                yield event.plain_result(
                    "压缩失败，请确认是视频或 GIF，并检查格式后重试。"
                )
        finally:
            if output_path:
                try:
                    Path(output_path).unlink(missing_ok=True)
                except OSError:
                    pass

    def _parse_kkgifzip_level(self, event: AstrMessageEvent) -> int:
        """从消息指令解析档位：/kkgifzip|/gifz → 1，/gifz3 → 3。"""
        raw = (event.get_message_str() or "").strip()
        first = raw.split()[0].lstrip("/") if raw.split() else ""
        plain = "".join(
            getattr(c, "text", "") or ""
            for c in event.get_messages()
            if isinstance(c, Comp.Plain)
        ).strip()
        token = plain.split()[0].lstrip("/") if plain.split() else first
        for candidate in (first, token):
            if not candidate:
                continue
            normalized = candidate.casefold()
            # 去掉末尾帮助后缀
            for suffix in ("帮助", "help", "?"):
                if normalized.endswith(suffix):
                    normalized = normalized[: -len(suffix)]
            for name in sorted(
                self._command_names_for_key("kkgifzip"),
                key=len,
                reverse=True,
            ):
                folded = name.casefold()
                if normalized == folded:
                    return 1
                if normalized.startswith(folded):
                    rest = normalized[len(folded) :]
                    if rest.isdigit() and 1 <= int(rest) <= 5:
                        return int(rest)
        return 1

    def _kkgifzip_help_text(self) -> str:
        names = self._command_names_for_key("kkgifzip")
        primary = f"/{names[0]}" if names else "/kkgifzip"
        extras = " ".join(f"/{n}" for n in names[1:3]) if len(names) > 1 else ""
        return (
            "GIF 压缩\n"
            f"用法：引用/附带视频或 GIF 后发 {primary}"
            + (f" 或 {extras}" if extras else "")
            + "\n"
            "裸指令 = 1 档；也可写 /指令3（1-5）。不支持静态图。\n"
            "视频最长 16 秒。"
        )

    async def _collect_videos(self, event: AstrMessageEvent) -> list:
        """Collect quoted videos first, then videos in the current message."""
        quoted: list = []
        current: list = []
        seen: set[str] = set()

        def add(target: list, component) -> None:
            if not isinstance(component, Comp.Video):
                return
            value = (
                getattr(component, "path", None)
                or getattr(component, "url", None)
                or getattr(component, "file", None)
            )
            marker = str(value or "").strip()
            if marker and marker not in seen:
                seen.add(marker)
                target.append(component)

        for component in event.get_messages():
            if isinstance(component, Comp.Video):
                add(current, component)
            elif isinstance(component, Comp.Reply):
                for quoted_component in component.chain or []:
                    add(quoted, quoted_component)
        videos = quoted + current
        logger.info("[kkt] kkgif 收集视频: count=%d", len(videos))
        return videos

    async def _collect_gif_images(self, event: AstrMessageEvent) -> list:
        """Collect quoted then current message images that look like GIF."""
        quoted: list = []
        current: list = []
        seen: set[str] = set()

        def marker_of(component) -> str:
            return str(
                getattr(component, "url", None)
                or getattr(component, "file", None)
                or getattr(component, "path", None)
                or ""
            ).strip()

        def looks_like_gif(component) -> bool:
            marker = marker_of(component).lower()
            if ".gif" in marker or "image/gif" in marker:
                return True
            for attr in ("type", "mime", "mime_type", "content_type"):
                value = str(getattr(component, attr, "") or "").lower()
                if "gif" in value:
                    return True
            return False

        def add(target: list, component) -> None:
            if not isinstance(component, Comp.Image):
                return
            if not looks_like_gif(component):
                return
            marker = marker_of(component)
            if marker and marker not in seen:
                seen.add(marker)
                target.append(component)

        for component in event.get_messages():
            if isinstance(component, Comp.Image):
                add(current, component)
            elif isinstance(component, Comp.Reply):
                for quoted_component in component.chain or []:
                    add(quoted, quoted_component)
        gifs = quoted + current
        logger.info("[kkt] kkgifzip 收集 GIF: count=%d", len(gifs))
        return gifs

    async def _count_static_images(self, event: AstrMessageEvent) -> int:
        """Count non-GIF images in quote + current message (for user-facing tip)."""
        count = 0
        seen: set[str] = set()

        def is_gif_like(component) -> bool:
            marker = str(
                getattr(component, "url", None)
                or getattr(component, "file", None)
                or getattr(component, "path", None)
                or ""
            ).lower()
            if ".gif" in marker or "image/gif" in marker:
                return True
            for attr in ("type", "mime", "mime_type", "content_type"):
                value = str(getattr(component, attr, "") or "").lower()
                if "gif" in value:
                    return True
            return False

        def consider(component) -> None:
            nonlocal count
            if not isinstance(component, Comp.Image):
                return
            marker = str(
                getattr(component, "url", None)
                or getattr(component, "file", None)
                or getattr(component, "path", None)
                or ""
            ).strip()
            if not marker or marker in seen:
                return
            seen.add(marker)
            if not is_gif_like(component):
                count += 1

        for component in event.get_messages():
            if isinstance(component, Comp.Image):
                consider(component)
            elif isinstance(component, Comp.Reply):
                for quoted_component in component.chain or []:
                    consider(quoted_component)
        return count

    async def _path_is_animated_gif(self, source_path: str) -> bool:
        path = Path(source_path)
        suffix = path.suffix.lower()
        try:
            with Image.open(path) as opened:
                fmt = str(opened.format or "").upper()
                frames = int(getattr(opened, "n_frames", 1) or 1)
                if fmt == "GIF" and frames > 1:
                    return True
                if frames > 1 and suffix in {".gif", ".webp"}:
                    return True
                if fmt == "GIF":
                    return True
        except Exception as exc:
            logger.debug("[kkt] GIF 探测失败 path=%s err=%s", source_path, exc)
            if suffix == ".gif":
                return True
            return False
        return suffix == ".gif"

    async def _probe_video_duration(self, source_path: str) -> float:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            source_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[:160]
            raise RuntimeError(f"ffprobe failed: {detail}")
        try:
            duration = float(stdout.decode("utf-8", errors="replace").strip())
        except ValueError as exc:
            raise RuntimeError("无法读取视频时长") from exc
        if duration <= 0:
            raise RuntimeError("视频时长无效")
        return duration

    async def _convert_video_to_gif(
        self, source_path: str
    ) -> tuple[str, float, int, int]:
        duration = await self._probe_video_duration(source_path)
        if duration > self.video_gif_max_duration + 0.05:
            raise RuntimeError("视频超过 16 秒")
        dimensions = []
        for value in (self.video_gif_max_dimension, 360, 256):
            if value not in dimensions:
                dimensions.append(value)
        fps_values = [self.video_gif_fps, 8, 6]
        last_error = "未知错误"
        for index, dimension in enumerate(dimensions):
            fps = fps_values[min(index, len(fps_values) - 1)]
            output_path = self.temp_dir / f"kkt_video_gif_{int(time.time() * 1000)}_{dimension}.gif"
            filter_graph = (
                f"[0:v]fps={fps},scale={dimension}:{dimension}:"
                "force_original_aspect_ratio=decrease:flags=lanczos,split[a][b];"
                "[a]palettegen=max_colors=256:stats_mode=diff[p];"
                "[b][p]paletteuse=dither=sierra2_4a"
            )
            command = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                source_path,
                "-t",
                str(min(duration, float(self.video_gif_max_duration))),
                "-filter_complex",
                filter_graph,
                "-an",
                "-loop",
                "0",
                str(output_path),
            ]
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
                if process.returncode != 0 or not output_path.is_file():
                    last_error = stderr.decode("utf-8", errors="replace")[:200]
                    output_path.unlink(missing_ok=True)
                    continue
                size = output_path.stat().st_size
                if size > self.video_gif_max_bytes:
                    logger.warning(
                        "[kkt] kkgif 输出过大，降低参数: dimension=%d fps=%d bytes=%d limit=%d",
                        dimension,
                        fps,
                        size,
                        self.video_gif_max_bytes,
                    )
                    output_path.unlink(missing_ok=True)
                    continue
                return str(output_path), duration, dimension, fps
            except asyncio.TimeoutError:
                output_path.unlink(missing_ok=True)
                last_error = "ffmpeg timeout"
            except FileNotFoundError as exc:
                raise RuntimeError("系统未安装 ffmpeg/ffprobe") from exc
        raise RuntimeError(f"GIF 转换失败: {last_error}")

    def _kkgifzip_filter_graph(
        self,
        *,
        dimension: int,
        fps: int,
        colors: int,
        crush: float,
        blur: float,
        dither: str,
        saturation: float = 1.2,
    ) -> str:
        """Build GIF filters: scale → crush → light blur → saturation → palette."""
        crush = max(1.0, float(crush))
        blur = max(0.0, float(blur))
        saturation = max(0.8, min(1.8, float(saturation)))
        # 最长边限制到 dimension，始终保持原比例；crush 只做等比缩小再放大，不拉方
        chain = (
            f"[0:v]fps={fps},"
            f"scale='min({dimension},iw)':'min({dimension},ih)'"
            f":force_original_aspect_ratio=decrease:flags=bilinear"
        )
        if crush > 1.05:
            chain += (
                f",scale=iw/{crush}:ih/{crush}:flags=bilinear,"
                f"scale=iw*{crush}:ih*{crush}:flags=neighbor"
            )
        if blur > 0.05:
            chain += f",gblur=sigma={blur}"
        # 提一点饱和度 + 对比，抵消 crush/模糊带来的发灰
        if abs(saturation - 1.0) > 0.02:
            chain += f",eq=saturation={saturation}:contrast=1.05"
        # stats_mode=diff：按帧差选色，比 single 少脏中间灰
        chain += (
            f",split[a][b];"
            f"[a]palettegen=max_colors={colors}:stats_mode=diff[p];"
            f"[b][p]paletteuse=dither={dither}"
        )
        return chain

    async def _convert_media_to_zip_gif(
        self,
        source_path: str,
        *,
        level: int,
        source_kind: str,
    ) -> tuple[str, float, int, int, int]:
        """Compress video or GIF to meme/JPG-like GIF by preset level (1-5)."""
        level = max(1, min(5, int(level)))
        preset = self._KKGIFZIP_PRESETS[level]
        base_dimension = int(preset["dimension"])
        base_fps = int(preset["fps"])
        base_colors = int(preset["colors"])
        base_crush = float(preset["crush"])
        base_blur = float(preset["blur"])
        base_saturation = float(preset.get("saturation", 1.2))
        base_dither = str(preset["dither"])

        if source_kind == "video":
            duration = await self._probe_video_duration(source_path)
            if duration > self.video_gif_max_duration + 0.05:
                raise RuntimeError("视频超过 16 秒")
            time_limit = min(duration, float(self.video_gif_max_duration))
        else:
            try:
                duration = await self._probe_video_duration(source_path)
            except Exception:
                duration = 0.0
            time_limit = None
            if duration > self.video_gif_max_duration + 0.05:
                time_limit = float(self.video_gif_max_duration)
                duration = time_limit

        # 超限时只再砍分辨率/色数/加强 crush，不降 fps（避免变卡）
        attempts: list[tuple[int, int, int, float, float, float, str]] = []
        for step in range(3):
            dimension = max(72, base_dimension - step * 36)
            colors = max(32, base_colors - step * 16)
            crush = min(6.0, base_crush + step * 0.6)
            blur = min(1.2, base_blur + step * 0.1)
            saturation = min(1.6, base_saturation + step * 0.05)
            dither = base_dither if step == 0 else "none"
            attempts.append(
                (dimension, base_fps, colors, crush, blur, saturation, dither)
            )

        last_error = "未知错误"
        for dimension, fps, colors, crush, blur, saturation, dither in attempts:
            stamp = int(time.time() * 1000)
            output_path = (
                self.temp_dir / f"kkt_gifzip_{stamp}_l{level}_{dimension}.gif"
            )
            filter_graph = self._kkgifzip_filter_graph(
                dimension=dimension,
                fps=fps,
                colors=colors,
                crush=crush,
                blur=blur,
                dither=dither,
                saturation=saturation,
            )
            command = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                source_path,
            ]
            if time_limit is not None:
                command.extend(["-t", str(time_limit)])
            command.extend(
                [
                    "-filter_complex",
                    filter_graph,
                    "-an",
                    "-loop",
                    "0",
                    str(output_path),
                ]
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
                if process.returncode != 0 or not output_path.is_file():
                    last_error = stderr.decode("utf-8", errors="replace")[:200]
                    output_path.unlink(missing_ok=True)
                    continue
                size = output_path.stat().st_size
                if size > self.video_gif_max_bytes:
                    logger.warning(
                        "[kkt] kkgifzip 输出过大: level=%d dimension=%d fps=%d "
                        "colors=%d crush=%.1f bytes=%d limit=%d",
                        level,
                        dimension,
                        fps,
                        colors,
                        crush,
                        size,
                        self.video_gif_max_bytes,
                    )
                    output_path.unlink(missing_ok=True)
                    continue
                return str(output_path), float(duration or 0.0), dimension, fps, colors
            except asyncio.TimeoutError:
                output_path.unlink(missing_ok=True)
                last_error = "ffmpeg timeout"
            except FileNotFoundError as exc:
                raise RuntimeError("系统未安装 ffmpeg/ffprobe") from exc
        raise RuntimeError(f"GIF 压缩失败: {last_error}")

    async def _handle_image_command(
        self, event: AstrMessageEvent, prompt: GreedyStr = ""
    ):
        group_id = str(event.get_group_id() or "").strip()
        if group_id and group_id in self.group_blacklist:
            logger.debug("[kkt] 忽略黑名单群消息: group_id=%s", group_id)
            return

        command = self._detect_command_name(event)
        is_gif = self._is_gif_command(command)
        prompt = self._extract_prompt(
            event, prompt, self._command_names_for_parser()
        )
        logger.info(
            "[kkt] 指令匹配: command=%s prompt=%r",
            command,
            prompt[:200],
        )
        event.stop_event()

        # 兼容：/kkt 额度、/kkt 额度 10、/kkt 重置额度 写在主指令参数里
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
            "统计",
        }:
            if not self._is_admin(event):
                yield event.plain_result("仅管理员可查询额度。")
                return
            yield event.plain_result(self._format_quota_status(event))
            return
        # 设置：额度10 / 额度 main 10 / quota image2=20
        set_match = re.fullmatch(
            r"(?:额度|限额|配额|quota|limit)\s*(.*)",
            prompt_stripped,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if set_match and normalized not in {
            "额度",
            "限额",
            "quota",
            "配额",
            "今日额度",
            "今日限额",
            "统计",
        }:
            rest = (set_match.group(1) or "").strip()
            if rest:
                if not self._is_admin(event):
                    yield event.plain_result("仅管理员可调整额度。")
                    return
                channel, new_limit, ok = self._parse_quota_command_arg(rest)
                if not ok or new_limit is None:
                    yield event.plain_result(
                        "参数无效。设置：/kkt 额度 10 或 /kkt 额度 main 100"
                    )
                    return
                target = channel or "all"
                old_limits = dict(self.channel_limits)
                await self._set_channel_quota_limit(target, new_limit)
                if target == "all":
                    head = f"两通道日限额 → {new_limit}"
                else:
                    label = self._CHANNEL_LABELS.get(target, target)
                    head = (
                        f"{label} 日限额 {old_limits.get(target, 0)} → {new_limit}"
                    )
                logger.info(
                    "[kkt] 管理员调整通道限额(主指令参数): operator=%s target=%s "
                    "limit=%d old=%s new=%s",
                    event.get_sender_id(),
                    target,
                    new_limit,
                    old_limits,
                    self.channel_limits,
                )
                yield event.plain_result(
                    head + "\n" + self._format_quota_status(event)
                )
                return
        if normalized in {
            "重置额度",
            "清零额度",
            "resetquota",
            "重置配额",
            "清零配额",
            "reset",
        } or re.fullmatch(
            r"(?:重置额度|清零额度|resetquota|重置配额|清零配额)\s*\S*",
            prompt_stripped,
            flags=re.IGNORECASE,
        ):
            if not self._is_admin(event):
                yield event.plain_result("仅管理员可重置额度。")
                return
            reset_rest = re.sub(
                r"(?i)^(?:重置额度|清零额度|resetquota|重置配额|清零配额)\s*",
                "",
                prompt_stripped,
            ).strip()
            ch = self._parse_channel_token(reset_rest) if reset_rest else "all"
            if reset_rest and ch is None:
                yield event.plain_result(
                    "参数无效。/kkt 重置额度 [all|main|image2]"
                )
                return
            await self._reset_channel_quota(ch or "all")
            yield event.plain_result(
                "已清零今日已用（累计次数保留）\n"
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
                yield event.plain_result("仅管理员可开关本地审核。")
                return
            old = self.sensitive_filter_enabled
            self._save_sensitive_filter_enabled(toggle)
            logger.info(
                "[kkt] 管理员切换本地审核(主指令参数): operator=%s old=%s new=%s words=%d",
                event.get_sender_id(),
                old,
                toggle,
                self._sensitive_word_count,
            )
            yield event.plain_result(self._format_sensitive_status())
            return

        # 先收集引用图文，再判断 help。
        # 否则裸 /kkt + 引用文案 会在读引用之前就被当成空 prompt 返回帮助。
        try:
            image_items, quoted_prompt = await self._collect_images(event)
        except Exception as exc:
            logger.warning(f"[kkt] 读取引用内容失败: {exc}")
            image_items, quoted_prompt = [], ""

        gif_help_requested = is_gif and self._is_help_token(prompt)
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
        if not prompt and not image_items and not (is_gif and not gif_help_requested):
            yield event.plain_result(self._gif_help_text if is_gif else self._help_text)
            return

        if command == "grok2" and image_items:
            logger.info(
                "[kkt] grok2 2K 多图/参考图拦截(不请求): count=%d labels=%s",
                len(image_items),
                [item.get("label") for item in image_items][:8],
            )
            yield event.plain_result(
                "/grok2 是 2K 文生图模式，不支持参考图。请使用 /grok 进行图生图。"
            )
            return

        # 本地 Sensitive-lexicon：三通道共用；命中则不请求、不扣配额
        sensitive_msg = self._check_sensitive_prompt(prompt)
        if sensitive_msg:
            yield event.plain_result(sensitive_msg)
            return

        api_command = self._gif_api_command(command) if is_gif else command
        gif_grid_size = self._gif_grid_size(command) if is_gif else 0
        creds = self._resolve_api_credentials(api_command)
        if isinstance(creds, str):
            logger.error("[kkt] 凭证未配置: command=%s", command)
            yield event.plain_result(creds)
            return
        api_base, api_keys, model = creds

        # image2 + Images API：多参考图会静默丢弃，直接拦截以免浪费额度
        if (
            api_command == "image2"
            and len(image_items) > 1
            and self._should_use_images_api(api_command, model)
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

        # 分通道日限额：仅检查，成功出图后再记账（失败不扣）
        billing_channel = self._channel_for_command(api_command)
        quota_msg = await self._check_channel_quota(event, billing_channel)
        if quota_msg:
            yield event.plain_result(quota_msg)
            return

        # 通过限流后再记 CD，避免配额/校验失败也吃 CD
        self._mark_user_cooldown(event)

        # 开始干活前先表情回应原消息（不阻塞生图）
        asyncio.create_task(self._send_reaction_emoji(event))
        # 普通消息提示进度，不引用原消息
        image_start_notice = "正在生成图片，马上就好喵"
        frame_notice = self._animated_reference_notice(image_items)
        if frame_notice:
            image_start_notice += f"\n{frame_notice}"
        await event.send(event.plain_result(image_start_notice))
        started_at = time.monotonic()
        task_id = self._start_task_log(
            channel=billing_channel,
            command=command,
            prompt=prompt,
            model=model,
        )

        try:
            logger.info(
                "[kkt] 开始调用图像 API: command=%s channel=%s model=%s key_count=%d "
                "image_count=%d label_images=%s image2_mode=%s",
                command,
                billing_channel,
                model,
                len(api_keys),
                len(image_items),
                self.label_images,
                self.image2_api_mode if command == "image2" else "n/a",
            )
            result = await self._request_image(
                self._build_gif_prompt(prompt, gif_grid_size) if is_gif else prompt,
                image_items,
                event,
                api_base=api_base,
                api_keys=api_keys,
                model=model,
                command=api_command,
            )
            if not result:
                logger.error("[kkt] API 调用完成但未解析出图片")
                self._finish_task_log(
                    task_id, status="failed", code="empty_response", progress=0
                )
                yield event.plain_result("API 返回中没有找到图片，请检查模型和接口响应格式。")
                return
            image_path = await self._materialize_image(result)
            if not image_path:
                self._finish_task_log(
                    task_id, status="failed", code="materialize_failed", progress=0
                )
                yield event.plain_result("图片下载或解析失败，请稍后重试。")
                return
            output_path = image_path
            if is_gif:
                output_path, frame_count, cell_size = self._make_gif_from_grid(
                    image_path, gif_grid_size
                )
                logger.info(
                    "[kkt] GIF 分镜裁切成功: source=%s grid=%dx%d frames=%d source_cell=%d output=%s",
                    image_path,
                    gif_grid_size,
                    gif_grid_size,
                    frame_count,
                    cell_size,
                    output_path,
                )
            await self._record_successful_usage(billing_channel)
            elapsed_seconds = max(1, int(round(time.monotonic() - started_at)))
            logger.info(
                "[kkt] 图片处理成功: path=%s elapsed=%ss channel=%s output=%s",
                output_path,
                elapsed_seconds,
                billing_channel,
                "gif" if is_gif else "image",
            )
            self._finish_task_log(
                task_id, status="success", code="completed", progress=100
            )
            yield event.chain_result(
                self._build_image_chain(
                    event,
                    output_path,
                    elapsed_seconds=elapsed_seconds,
                )
            )
            self._schedule_cleanup(output_path)
            if output_path != image_path:
                self._schedule_cleanup(image_path)
        except Exception as exc:
            logger.exception("[kkt] 图片生成失败: %s", exc)
            self._finish_task_log(
                task_id, status="failed", code=type(exc).__name__, progress=0
            )
            yield event.plain_result(self._safe_image_failure(exc))

    @filter.command("hajimi", alias={"kkt"})
    async def handle_hajimi(self, event: AstrMessageEvent, prompt: GreedyStr = ""):
        """主图像通道：文生图、修图、多图参考和引用图编辑。用法：/hajimi <提示词>。"""
        async for result in self._handle_image_command(event, prompt):
            yield result

    @filter.command("image2")
    async def handle_image2(self, event: AstrMessageEvent, prompt: GreedyStr = ""):
        """Image2 独立通道：Images 模式最多一张参考图。用法：/image2 <提示词>。"""
        async for result in self._handle_image_command(event, prompt):
            yield result

    @filter.command("grok", alias={"gk"})
    async def handle_grok(self, event: AstrMessageEvent, prompt: GreedyStr = ""):
        """Grok Images 文生图/图生图，支持多图参考。用法：/grok <提示词>。"""
        async for result in self._handle_image_command(event, prompt):
            yield result

    @filter.command("grok2", alias={"grok2k", "gk2", "gk2k"})
    async def handle_grok2(self, event: AstrMessageEvent, prompt: GreedyStr = ""):
        """Grok 2K 文生图，仅支持文字提示词，不接受参考图。用法：/grok2 <提示词>。"""
        async for result in self._handle_image_command(event, prompt):
            yield result

    @filter.command("hajimigif", alias={"kktgif"})
    async def handle_hajimigif(
        self, event: AstrMessageEvent, prompt: GreedyStr = ""
    ):
        """主通道生成 4x4、16 帧 GIF 分镜。用法：/hajimigif <动作>。"""
        async for result in self._handle_image_command(event, prompt):
            yield result

    @filter.command("hajimigif2")
    async def handle_hajimigif2(
        self, event: AstrMessageEvent, prompt: GreedyStr = ""
    ):
        """主通道生成 3x3、9 帧 GIF 分镜。用法：/hajimigif2 <动作>。"""
        async for result in self._handle_image_command(event, prompt):
            yield result

    @filter.command("image2gif")
    async def handle_image2gif(
        self, event: AstrMessageEvent, prompt: GreedyStr = ""
    ):
        """Image2 通道生成 4x4、16 帧 GIF 分镜。用法：/image2gif <动作>。"""
        async for result in self._handle_image_command(event, prompt):
            yield result

    @filter.command("image2gif2")
    async def handle_image2gif2(
        self, event: AstrMessageEvent, prompt: GreedyStr = ""
    ):
        """Image2 通道生成 3x3、9 帧 GIF 分镜。用法：/image2gif2 <动作>。"""
        async for result in self._handle_image_command(event, prompt):
            yield result

    @filter.command("kkt帮助", alias=_DEFAULT_HELP_ALIASES)
    async def handle_help(self, event: AstrMessageEvent):
        """帮助卡：优先 T2I 图片，失败回退精简纯文本。"""
        event.stop_event()
        groups = self._command_help_groups()
        md_text = build_user_help_markdown(groups)
        plain_text = build_user_help_plain(groups)
        # 1) 本地 T2I（不依赖全局 t2i 开关）
        image_path = await self._render_help_t2i(md_text)
        if image_path:
            try:
                try:
                    image = Comp.Image.fromFileSystem(str(Path(image_path).resolve()))
                except Exception:
                    image = Comp.Image(
                        file=str(Path(image_path).resolve()), path=image_path
                    )
                await event.send(MessageChain([image]))
                self._schedule_cleanup(image_path)
                return
            except Exception as exc:
                logger.warning("[kkt] 帮助图发送失败，回退文本: %s", exc)
        # 2) 纯文本（结构与图一致）
        yield event.plain_result(plain_text)

    async def _render_help_t2i(self, markdown_text: str) -> str | None:
        """Render help markdown to a local image path; None on failure."""
        try:
            from astrbot.api import html_renderer
        except Exception as exc:
            logger.debug("[kkt] html_renderer 不可用: %s", exc)
            return None
        try:
            path = await html_renderer.render_t2i(
                markdown_text,
                use_network=False,
                return_url=False,
                template_name="base",
            )
            if path and Path(str(path)).is_file():
                logger.info("[kkt] 帮助 T2I 成功: path=%s", path)
                return str(path)
        except Exception as exc:
            logger.warning("[kkt] 本地 T2I 失败，尝试远端: %s", exc)
        try:
            path = await html_renderer.render_t2i(
                markdown_text,
                use_network=True,
                return_url=False,
                template_name="base",
            )
            if path and Path(str(path)).is_file():
                logger.info("[kkt] 帮助 T2I 远端成功: path=%s", path)
                return str(path)
        except Exception as exc:
            logger.warning("[kkt] 帮助 T2I 失败: %s", exc)
        return None

    @filter.command(
        "grokvideo",
        alias={"grokv", "gkv", "gv"}
        | {f"grokv{i}" for i in range(100)}
        | {f"gkv{i}" for i in range(100)}
        | {f"gv{i}" for i in range(100)},
    )
    async def handle_grokv(self, event: AstrMessageEvent, prompt: GreedyStr = ""):
        """Grok2API 异步文生/图生视频，支持 1-15 秒和一张首帧参考图。"""
        group_id = str(event.get_group_id() or "").strip()
        if group_id and group_id in self.group_blacklist:
            logger.debug("[kkt] video 忽略黑名单群: group_id=%s", group_id)
            return

        event.stop_event()
        prompt = self._extract_prompt(
            event, prompt, self._command_names_for_parser()
        )
        duration_seconds, prompt, duration_error = self._parse_grokv_duration(
            event,
            prompt,
            self.video_duration,
            self._command_names_for_key("video"),
        )
        if duration_error:
            yield event.plain_result(duration_error)
            return
        prompt_stripped = (prompt or "").strip()
        if self._is_help_token(prompt_stripped) or prompt_stripped.lower() in {
            "help",
            "帮助",
            "?",
        }:
            yield event.plain_result(self._video_help_text)
            return

        image_items, quoted_prompt = await self._collect_images(event)
        if not prompt_stripped and quoted_prompt:
            prompt_stripped = quoted_prompt.strip()
            prompt = prompt_stripped

        if not prompt_stripped and not image_items:
            yield event.plain_result(self._video_help_text)
            return

        if len(image_items) > 1:
            logger.info(
                "[kkt] video multi-ref reject: count=%d sources=%s",
                len(image_items),
                [item.get("source") for item in image_items][:8],
            )
            yield event.plain_result(
                f"每次只能用一张参考图作为首帧，当前收到 {len(image_items)} 张。"
                "请只保留一张图（附图、回复图或@头像）后重试。"
            )
            return

        sensitive_msg = self._check_sensitive_prompt(prompt_stripped)
        if sensitive_msg:
            yield event.plain_result(sensitive_msg)
            return

        if not self.video_api_base:
            yield event.plain_result(
                "未配置 video_api_base。请在插件配置中填写 grok2api 地址。"
            )
            return
        video_keys = self._build_key_chain(
            self.video_api_key, self.video_backup_api_keys
        )
        if not video_keys:
            yield event.plain_result(
                "未配置 video_api_key。请在插件配置中填写 grok2api 客户端密钥。"
            )
            return

        cd_msg = self._check_video_cooldown(event)
        if cd_msg:
            yield event.plain_result(cd_msg)
            return

        billing_channel = self._CHANNEL_VIDEO
        quota_msg = await self._check_channel_quota(event, billing_channel)
        if quota_msg:
            yield event.plain_result(quota_msg)
            return

        ok, slot_msg = await self._try_acquire_video_slot(event)
        if not ok:
            yield event.plain_result(slot_msg or "视频队列已满，请稍后再试。")
            return

        self._mark_video_cooldown(event)
        asyncio.create_task(self._send_reaction_emoji(event))
        video_start_notice = "喵呜～视频已经开始生成啦，可能要等几分钟，请耐心等我一下喵～"
        frame_notice = self._animated_reference_notice(image_items)
        if frame_notice:
            video_start_notice += f"\n{frame_notice}"
        try:
            await event.send(event.plain_result(video_start_notice))
        except Exception as exc:
            logger.warning("[kkt] video start notice send failed, continue task: %s", exc)
        image_url = None
        if image_items:
            image_url = str(image_items[0].get("data_url") or "").strip() or None
        final_prompt = self._compose_video_prompt(
            prompt_stripped,
            has_ref_image=bool(image_url),
            duration=duration_seconds,
        )
        started_at = time.monotonic()
        task_id = self._start_task_log(
            channel=billing_channel,
            command="grokvideo",
            prompt=prompt_stripped or final_prompt,
            model=self.video_model,
        )

        async def on_video_progress(progress: int, status: str) -> None:
            if status == "pending":
                self._update_task_log(task_id, progress=progress)

        try:
            video_aspect_ratio = (
                self._video_aspect_ratio_for_image(image_items[0])
                if image_items else self.video_aspect_ratio
            )
            logger.info(
                "[kkt] grokvideo start: model=%s base=%s duration=%s ar=%s res=%s "
                "prompt_len=%d final_prompt_len=%d enhance=%s has_image=%s "
                "keys=%d timeout=%ds",
                self.video_model,
                self.video_api_base,
                duration_seconds,
                video_aspect_ratio,
                self.video_resolution,
                len(prompt_stripped),
                len(final_prompt),
                self.video_prompt_enhance,
                bool(image_url),
                len(video_keys),
                self.video_timeout,
            )
            client = GrokVideoClient(
                self.video_api_base,
                poll_interval=float(self.video_poll_interval),
                timeout=float(self.video_timeout),
            )
            result, content, ctype = await client.generate(
                video_keys,
                model=self.video_model,
                prompt=final_prompt,
                duration=duration_seconds,
                aspect_ratio=video_aspect_ratio,
                resolution=self.video_resolution,
                image_url=image_url,
                on_progress=on_video_progress,
            )
            raw_path = await self._materialize_video_bytes(content, ctype)
            video_path = await self._transcode_video_for_qq(raw_path)
            await self._record_successful_usage(billing_channel)
            elapsed_seconds = max(1, int(round(time.monotonic() - started_at)))
            send_size = (
                Path(video_path).stat().st_size
                if Path(video_path).is_file()
                else len(content)
            )
            logger.info(
                "[kkt] video success: request_id=%s raw=%s send=%s bytes=%d elapsed=%ss",
                result.request_id,
                raw_path,
                video_path,
                send_size,
                elapsed_seconds,
            )
            # link_resolver Direct Send：await event.send，不 yield 进装饰链
            try:
                await self._send_video_direct(
                    event,
                    video_path,
                    elapsed_seconds=elapsed_seconds,
                )
                self._finish_task_log(
                    task_id,
                    status="success",
                    code="completed",
                    progress=100,
                    request_id=result.request_id,
                )
            finally:
                # 发送完成（或失败）后再删，避免边发边删
                for p in {raw_path, video_path}:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except OSError:
                        pass
                logger.debug(
                    "[kkt] video temp cleaned after send: raw=%s send=%s",
                    raw_path,
                    video_path,
                )
        except GrokVideoError as exc:
            logger.error(
                "[kkt] video failed: code=%s status=%s msg=%s",
                exc.code,
                exc.status,
                str(exc)[:300],
            )
            self._finish_task_log(
                task_id,
                status="failed",
                code=exc.code or "generation_failed",
                progress=0,
            )
            yield event.plain_result(self._safe_video_failure(exc))
        except Exception as exc:
            logger.exception("[kkt] video unexpected error: %s", exc)
            self._finish_task_log(
                task_id, status="failed", code=type(exc).__name__, progress=0
            )
            yield event.plain_result("视频生成失败，请稍后重试。")
        finally:
            await self._release_video_slot(event)

    @filter.command(
        "grokpack",
        alias={
            "gkpack",
            "gkp",
            "grokpackz",
            "gkpackz",
            "gkpz",
            "grokpackz1",
            "grokpackz2",
            "grokpackz3",
            "grokpackz4",
            "grokpackz5",
            "gkpackz1",
            "gkpackz2",
            "gkpackz3",
            "gkpackz4",
            "gkpackz5",
            "gkpz1",
            "gkpz2",
            "gkpz3",
            "gkpz4",
            "gkpz5",
        },
    )
    async def handle_grokpack(self, event: AstrMessageEvent, prompt: GreedyStr = ""):
        """Grok 全套：图→视频→GIF。过程合并转发，最终 GIF 单独发。"""
        async for result in self._handle_grok_workflow(
            event, prompt, mode="pack"
        ):
            yield result

    @filter.command(
        "grokvg",
        alias={
            "gkvg",
            "gvg",
            "grokvgz",
            "gkvgz",
            "gvgz",
            "grokvgz1",
            "grokvgz2",
            "grokvgz3",
            "grokvgz4",
            "grokvgz5",
            "gkvgz1",
            "gkvgz2",
            "gkvgz3",
            "gkvgz4",
            "gkvgz5",
            "gvgz1",
            "gvgz2",
            "gvgz3",
            "gvgz4",
            "gvgz5",
        },
    )
    async def handle_grokvg(self, event: AstrMessageEvent, prompt: GreedyStr = ""):
        """Grok 视频套：视频→GIF。过程合并转发，最终 GIF 单独发。"""
        async for result in self._handle_grok_workflow(event, prompt, mode="vg"):
            yield result

    def _parse_workflow_zip_level(self, event: AstrMessageEvent, mode: str) -> int | None:
        """解析工作流压缩档：无 z → None（普通 kkgif）；z/zN → 1-5。"""
        raw = (event.get_message_str() or "").strip()
        first = raw.split()[0].lstrip("/") if raw.split() else ""
        plain = "".join(
            getattr(c, "text", "") or ""
            for c in event.get_messages()
            if isinstance(c, Comp.Plain)
        ).strip()
        token = (plain.split()[0].lstrip("/") if plain.split() else first).casefold()
        for suffix in ("帮助", "help", "?"):
            if token.endswith(suffix):
                token = token[: -len(suffix)]
        key = "grokpack" if mode == "pack" else "grokvg"
        for name in sorted(
            self._command_names_for_key(key), key=len, reverse=True
        ):
            folded = name.casefold()
            if token == folded:
                if folded in self._WORKFLOW_ZIP_BASES or folded.endswith("z"):
                    return 1
                return None
            if token.startswith(folded):
                rest = token[len(folded) :]
                if rest.isdigit() and 1 <= int(rest) <= 5:
                    return int(rest)
        # 兜底：token 本身带 z
        match = re.match(r"^.+?z([1-5])?$", token)
        if match:
            return int(match.group(1) or 1)
        return None

    def _compose_workflow_still_prompt(
        self, user_prompt: str, *, has_ref_image: bool
    ) -> str:
        """用户意图是视频；生图时明确「这张图要当视频首帧」。"""
        body = (user_prompt or "").strip() or "一段简短自然的动态画面"
        ref_line = (
            "已有参考图时：在保持参考图主体外观与画风的前提下，调整构图与姿态，"
            "使画面适合作为视频第一帧，便于后续动作展开。"
            if has_ref_image
            else "无参考图时：按用户描述构建清晰主体与场景，构图适合作为视频第一帧。"
        )
        return (
            "【工作流·视频首帧静帧】\n"
            "用户描述的是期望的视频内容与动作，不是普通插画。\n"
            "请生成一张适合作为视频首帧的静帧：主体完整、姿态自然、构图留有运动空间，"
            "便于下一阶段图生视频从这一帧开始动起来。\n"
            f"{ref_line}\n"
            "不要做成分镜网格、连环画或多格；单张画面即可。"
            "不要添加字幕、水印、UI。\n\n"
            f"用户期望的视频内容：{body}"
        )

    def _workflow_help_plain(self, mode: str) -> str:
        if mode == "pack":
            return (
                "Grok 全套工作流\n"
                "用法：/grokpack <视频意图提示词>\n"
                "压缩成品：/grokpackz 或 /gkpz1-5\n"
                "取图逻辑同 /grok（附图/引用/@头像）\n"
                "流程：图(首帧)→视频→GIF；过程合并转发，成品单独发"
            )
        return (
            "Grok 视频+GIF 工作流\n"
            "用法：/grokvg <提示词>\n"
            "压缩成品：/grokvgz 或 /gvgz1-5\n"
            "取图逻辑同 /grokvideo（最多一张首帧）\n"
            "流程：视频→GIF；过程合并转发，成品单独发"
        )

    async def _handle_grok_workflow(
        self,
        event: AstrMessageEvent,
        prompt: GreedyStr,
        *,
        mode: str,
    ):
        """mode=pack：图→视频→GIF；mode=vg：视频→GIF。

        发送：① 合并转发(过程提示+过程产物) ② 单独发最终 GIF。
        """
        group_id = str(event.get_group_id() or "").strip()
        if group_id and group_id in self.group_blacklist:
            return

        event.stop_event()
        zip_level = self._parse_workflow_zip_level(event, mode)
        prompt = self._extract_prompt(
            event, prompt, self._command_names_for_parser()
        )
        duration_seconds, prompt, duration_error = self._parse_grokv_duration(
            event,
            prompt,
            self.video_duration,
            self._command_names_for_key("video")
            + self._command_names_for_key(
                "grokpack" if mode == "pack" else "grokvg",
                include_level_aliases=True,
            ),
        )
        if duration_error:
            yield event.plain_result(duration_error)
            return

        prompt_stripped = (prompt or "").strip()
        if self._is_help_token(prompt_stripped) or prompt_stripped.lower() in {
            "help",
            "帮助",
            "?",
        }:
            yield event.plain_result(self._workflow_help_plain(mode))
            return

        try:
            image_items, quoted_prompt = await self._collect_images(event)
        except Exception as exc:
            logger.warning("[kkt] workflow 读图失败: %s", exc)
            image_items, quoted_prompt = [], ""
        if not prompt_stripped and quoted_prompt:
            prompt_stripped = quoted_prompt.strip()
            prompt = prompt_stripped

        if not prompt_stripped and not image_items:
            yield event.plain_result(self._workflow_help_plain(mode))
            return

        if mode == "vg" and len(image_items) > 1:
            yield event.plain_result(
                f"视频套每次只能用一张首帧参考图，当前 {len(image_items)} 张。"
            )
            return

        # pack 生图阶段：grok 可多图；视频阶段只用第一张/生成图
        sensitive_msg = self._check_sensitive_prompt(prompt_stripped)
        if sensitive_msg:
            yield event.plain_result(sensitive_msg)
            return

        if not self.video_api_base:
            yield event.plain_result(
                "未配置 video_api_base。请在插件配置中填写 grok2api 地址。"
            )
            return
        video_keys = self._build_key_chain(
            self.video_api_key, self.video_backup_api_keys
        )
        if not video_keys:
            yield event.plain_result(
                "未配置 video_api_key。请在插件配置中填写 grok2api 客户端密钥。"
            )
            return

        if mode == "pack":
            creds = self._resolve_api_credentials("grok")
            if isinstance(creds, str):
                yield event.plain_result(creds)
                return
            api_base, api_keys, model = creds
            cd_msg = self._check_user_cooldown(event)
            if cd_msg:
                yield event.plain_result(cd_msg)
                return
            # grok 记账通道与 /grok 一致（当前归 main）
            billing_image = self._channel_for_command("grok")
            quota_img = await self._check_channel_quota(event, billing_image)
            if quota_img:
                yield event.plain_result(quota_img)
                return
        else:
            api_base, api_keys, model = "", [], ""
            billing_image = ""

        quota_video = await self._check_channel_quota(event, self._CHANNEL_VIDEO)
        if quota_video:
            yield event.plain_result(quota_video)
            return

        ok, slot_msg = await self._try_acquire_video_slot(event)
        if not ok:
            yield event.plain_result(slot_msg or "视频队列已满，请稍后再试。")
            return

        if mode == "pack":
            self._mark_user_cooldown(event)
        self._mark_video_cooldown(event)
        asyncio.create_task(self._send_reaction_emoji(event))

        zip_hint = f"·压缩{zip_level}档" if zip_level else ""
        try:
            await event.send(
                event.plain_result(
                    f"喵～工作流启动{zip_hint}，做好后一起端上来，请等一下哦～"
                )
            )
        except Exception:
            pass

        process_nodes: list = []
        self_id = str(event.get_self_id() or "0")
        image_path = ""
        video_path = ""
        raw_path = ""
        gif_path = ""
        image_url: str | None = None
        image_items_for_video: list[dict] = []
        cleanup_paths: list[str] = []
        task_id = self._start_task_log(
            channel="workflow",
            command=f"{'grokpack' if mode == 'pack' else 'grokvg'}"
            + (f"z{zip_level}" if zip_level else ""),
            prompt=prompt_stripped,
            model="workflow",
        )

        def add_plain_node(text: str, name: str = "康康喵") -> None:
            process_nodes.append(
                Comp.Node(content=[Comp.Plain(text)], name=name, uin=self_id)
            )

        def add_image_node(path: str, name: str = "康康喵") -> None:
            process_nodes.append(
                Comp.Node(
                    content=[Comp.Image(file=str(Path(path).resolve()))],
                    name=name,
                    uin=self_id,
                )
            )

        def add_video_node(path: str, name: str = "康康喵") -> None:
            try:
                video_comp = self._make_video_component(path)
            except Exception:
                video_comp = Comp.Video(file=str(Path(path).resolve()))
            process_nodes.append(
                Comp.Node(content=[video_comp], name=name, uin=self_id)
            )

        try:
            # ── 1) 全套：生图（视频首帧意图）──
            if mode == "pack":
                add_plain_node("喵～先去画一张适合当视频首帧的参考图，请稍等～")
                still_prompt = self._compose_workflow_still_prompt(
                    prompt_stripped, has_ref_image=bool(image_items)
                )
                result = await self._request_image(
                    still_prompt,
                    image_items,
                    event,
                    api_base=api_base,
                    api_keys=api_keys,
                    model=model,
                    command="grok",
                )
                if not result:
                    add_plain_node("呜…参考图没画出来，工作流中断了喵。")
                    await self._send_workflow_process_forward(event, process_nodes)
                    self._finish_task_log(
                        task_id, status="failed", code="empty_image", progress=0
                    )
                    yield event.plain_result("参考图生成失败，请稍后重试。")
                    return
                image_path = await self._materialize_image(result)
                if not image_path:
                    add_plain_node("呜…参考图下载失败了喵。")
                    await self._send_workflow_process_forward(event, process_nodes)
                    self._finish_task_log(
                        task_id, status="failed", code="materialize_image", progress=0
                    )
                    yield event.plain_result("参考图下载失败，请稍后重试。")
                    return
                cleanup_paths.append(image_path)
                await self._record_successful_usage(billing_image)
                add_plain_node("参考图好啦，这张会当作视频首帧用哦～")
                add_image_node(image_path)
                # 视频首帧：用生成图
                try:
                    raw_bytes = Path(image_path).read_bytes()
                    encoded = base64.b64encode(raw_bytes).decode("ascii")
                    suffix = Path(image_path).suffix.lower()
                    mime = {
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".webp": "image/webp",
                        ".gif": "image/gif",
                    }.get(suffix, "image/png")
                    image_url = f"data:{mime};base64,{encoded}"
                    image_items_for_video = [
                        {"data_url": image_url, "source": "workflow_still"}
                    ]
                except Exception as exc:
                    logger.warning("[kkt] workflow still→data_url 失败: %s", exc)
                    image_items_for_video = image_items[:1] if image_items else []
                    image_url = (
                        str(image_items_for_video[0].get("data_url") or "").strip()
                        or None
                        if image_items_for_video
                        else None
                    )
            else:
                image_items_for_video = image_items[:1]
                image_url = (
                    str(image_items_for_video[0].get("data_url") or "").strip() or None
                    if image_items_for_video
                    else None
                )

            # ── 2) 视频 ──
            add_plain_node("接着去做视频啦，可能要等一会儿，蹭蹭～")
            final_prompt = self._compose_video_prompt(
                prompt_stripped,
                has_ref_image=bool(image_url),
                duration=duration_seconds,
            )
            video_aspect_ratio = (
                self._video_aspect_ratio_for_image(image_items_for_video[0])
                if image_items_for_video
                else self.video_aspect_ratio
            )
            client = GrokVideoClient(
                self.video_api_base,
                poll_interval=float(self.video_poll_interval),
                timeout=float(self.video_timeout),
            )
            result, content, ctype = await client.generate(
                video_keys,
                model=self.video_model,
                prompt=final_prompt,
                duration=duration_seconds,
                aspect_ratio=video_aspect_ratio,
                resolution=self.video_resolution,
                image_url=image_url or None,
            )
            raw_path = await self._materialize_video_bytes(content, ctype)
            video_path = await self._transcode_video_for_qq(raw_path)
            cleanup_paths.extend([raw_path, video_path])
            await self._record_successful_usage(self._CHANNEL_VIDEO)
            add_plain_node("视频做好了，请先在这里看看过程喵～")
            add_video_node(video_path)

            # ── 3) GIF（最终成品）──
            add_plain_node(
                f"最后捏成表情包"
                + (f"（{zip_level} 档）" if zip_level else "")
                + "，马上就好～"
            )
            if zip_level:
                gif_path, _, _, _, _ = await self._convert_media_to_zip_gif(
                    video_path, level=zip_level, source_kind="video"
                )
            else:
                gif_path, _, _, _ = await self._convert_video_to_gif(video_path)
            cleanup_paths.append(gif_path)
            add_plain_node("过程都在上面啦，成品表情包单独发给主人～")

            await self._send_workflow_process_forward(event, process_nodes)
            # 最终成品单独发
            try:
                image = Comp.Image.fromFileSystem(str(Path(gif_path).resolve()))
            except Exception:
                image = Comp.Image(
                    file=str(Path(gif_path).resolve()), path=gif_path
                )
            await event.send(MessageChain([image]))
            self._finish_task_log(
                task_id, status="success", code="completed", progress=100
            )
            logger.info(
                "[kkt] workflow ok: mode=%s zip=%s image=%s video=%s gif=%s",
                mode,
                zip_level,
                bool(image_path),
                bool(video_path),
                bool(gif_path),
            )
        except GrokVideoError as exc:
            logger.error("[kkt] workflow video failed: %s", exc)
            add_plain_node(f"呜…视频这一步失败了：{self._safe_video_failure(exc)}")
            await self._send_workflow_process_forward(event, process_nodes)
            self._finish_task_log(
                task_id, status="failed", code=exc.code or "video_failed", progress=0
            )
            yield event.plain_result(self._safe_video_failure(exc))
        except Exception as exc:
            logger.exception("[kkt] workflow failed: %s", exc)
            add_plain_node("呜…工作流中途出错了，请主人稍后再试喵。")
            await self._send_workflow_process_forward(event, process_nodes)
            self._finish_task_log(
                task_id, status="failed", code=type(exc).__name__, progress=0
            )
            if "16 秒" in str(exc):
                yield event.plain_result("视频超过 16 秒，无法转 GIF。")
            else:
                yield event.plain_result("工作流失败，请稍后重试。")
        finally:
            await self._release_video_slot(event)
            for p in cleanup_paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass

    async def _send_workflow_process_forward(
        self, event: AstrMessageEvent, nodes: list
    ) -> None:
        """发送过程合并转发；失败则降级为逐条文本提示。"""
        if not nodes:
            return
        try:
            await event.send(MessageChain([Comp.Nodes(nodes)]))
        except Exception as exc:
            logger.warning("[kkt] workflow 合并转发失败，降级文本: %s", exc)
            for node in nodes:
                content = getattr(node, "content", None) or []
                texts = [
                    getattr(c, "text", "")
                    for c in content
                    if isinstance(c, Comp.Plain) and getattr(c, "text", "")
                ]
                if texts:
                    try:
                        await event.send(event.plain_result("\n".join(texts)))
                    except Exception:
                        pass

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
            data_url = f"data:image/jpeg;base64,{encoded}"
            animated_frame = ""
            try:
                raw_bytes = base64.b64decode(encoded)
                with Image.open(io.BytesIO(raw_bytes)) as opened:
                    frame_count = int(getattr(opened, "n_frames", 1) or 1)
                    if frame_count > 1:
                        if self.animated_reference_frame == "末帧":
                            frame_index = frame_count - 1
                        elif self.animated_reference_frame == "中间帧":
                            frame_index = (frame_count - 1) // 2
                        else:
                            frame_index = 0
                        opened.seek(frame_index)
                        frame = opened.convert("RGBA")
                        buffer = io.BytesIO()
                        frame.save(buffer, format="PNG")
                        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                        data_url = f"data:image/png;base64,{encoded}"
                        animated_frame = (
                            f"{self.animated_reference_frame}"
                            f"（第{frame_index + 1}/{frame_count}帧）"
                        )
                        logger.info(
                            "[kkt] 动图参考图抽帧: source=%s mode=%s frame=%d/%d",
                            source,
                            self.animated_reference_frame,
                            frame_index + 1,
                            frame_count,
                        )
            except Exception as exc:
                logger.debug("[kkt] 参考图动图检测失败，保留原始数据: %s", exc)
            item = {
                "data_url": data_url,
                "source": source,
                "qq": qq,
                "name": name,
            }
            if animated_frame:
                item["animated_frame"] = animated_frame
            images.append(
                item
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

    @staticmethod
    def _default_video_prompt_for_ref() -> str:
        """空 prompt + 仅参考图时的中性默认指令（人/动物/静物/风景通用）。"""
        return (
            "参考图中的主体自然轻微动起来，镜头稳定；"
            "有生物则做轻微呼吸、眨眼或摆动等小动作，"
            "是静物或风景则做轻风、光影、云雾等缓慢环境变化；"
            "不要擅自添加新角色或改掉主体外观。"
        )

    def _compose_video_prompt(
        self,
        prompt: str,
        *,
        has_ref_image: bool = False,
        duration: int | None = None,
    ) -> str:
        """为 /grokvideo 拼装静态前缀 + 用户指令（与生图 style_prompt 分离）。"""
        body = (prompt or "").strip()
        if not body:
            body = (
                self._default_video_prompt_for_ref()
                if has_ref_image
                else "请生成一段简短自然的视频"
            )
        if not self.video_prompt_enhance:
            return body

        dur = max(1, min(15, int(duration or self.video_duration or 8)))
        parts: list[str] = []
        if has_ref_image:
            subject_line = (
                "1) 主体一致：严格以参考图为首帧，还原主体外观、画风、材质与场景，"
                "只增加合理动作与镜头运动；不要擅自添加新角色或改掉主体。"
            )
        else:
            subject_line = (
                "1) 主体一致：全程保持同一主体的外观与身份，按用户文字构建场景。"
            )
        parts.append(
            "【视频生成约束】"
            f"{subject_line}"
            "2) 运动连贯：动作物理可信，禁止瞬移、结构崩坏、多头多肢、脸部/形体融化。"
            "3) 镜头稳定：以稳定画面为主，可轻推/轻摇；避免剧烈抖动、乱切、闪帧。"
            f"4) 时序：约 {dur} 秒内完成用户描述的动作，节奏自然，可轻微缓入缓出。"
            "5) 画面干净：不无故添加字幕、水印、UI 边框；用户明确要求文字时再出现。"
        )
        if self.prefer_chinese_text:
            parts.append(
                "【画面文字】若出现可读文字（字幕、招牌、UI），默认简体中文；"
                "仅当用户明确要求其他语言或专有名词需保留原文时除外。"
            )
        if self.prefer_cn_locale and not has_ref_image:
            parts.append(
                "【人物默认·仅文生且未指定时】人物外貌可略偏东亚常见特征；"
                "用户已指定种族/角色/画风时以用户为准。有参考图时本条不适用。"
            )
        custom = str(getattr(self, "video_style_prompt", "") or "").strip()
        if custom:
            parts.append(custom)
        prefix = "\n".join(parts).strip()
        return f"{prefix}\n\n用户指令：{body}"

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

        # Grok 图片模型必须走 grok2api 的 Images API；该接口支持 images 数组多图参考。
        if command == "grok":
            return await self._request_grok_image_via_images_api(
                prompt,
                image_items,
                api_base=use_base,
                api_keys=key_chain,
                resolution="1k",
            )
        if command == "grok2":
            return await self._request_grok_image_via_images_api(
                prompt,
                image_items,
                api_base=use_base,
                api_keys=key_chain,
                resolution="2k",
            )

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

    async def _request_grok_image_via_images_api(
        self,
        prompt: str,
        image_items: list[dict],
        *,
        api_base: str,
        api_keys: list[str],
        resolution: str = "1k",
    ) -> str | None:
        """Call grok2api's JSON image generation/edit endpoints.

        The public model list hides provider prefixes, while Chat Completions
        may resolve the same bare name to the wrong provider. Images API is the
        actual capability contract for this model and accepts multiple images.
        """
        endpoint = (
            f"{api_base.rstrip('/')}/images/edits"
            if image_items
            else f"{api_base.rstrip('/')}/images/generations"
        )
        body: dict[str, object] = {
            "model": self._GROK_IMAGE_MODEL,
            "prompt": self._compose_images_prompt(prompt),
            "n": 1,
            "resolution": resolution,
            "response_format": "url",
            "stream": False,
        }
        if image_items:
            body["images"] = [
                {"url": str(item.get("data_url") or "")}
                for item in image_items
            ]

        last_error = "未知错误"
        logger.info(
            "[kkt] grok images 请求准备: endpoint=%s model=%s key_count=%d "
            "ref_images=%d prompt_length=%d",
            endpoint,
            self._GROK_IMAGE_MODEL,
            len(api_keys),
            len(image_items),
            len(str(body["prompt"])),
        )
        for key_index, use_key in enumerate(api_keys):
            key_label = "primary" if key_index == 0 else f"backup#{key_index}"
            key_mask = self._mask_secret(use_key)
            for attempt in range(self.max_retry + 1):
                try:
                    timeout = aiohttp.ClientTimeout(total=self.timeout)
                    headers = {
                        "Authorization": f"Bearer {use_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    }
                    logger.info(
                        "[kkt] grok images API 发送: key=%s mask=%s attempt=%d/%d",
                        key_label,
                        key_mask,
                        attempt + 1,
                        self.max_retry + 1,
                    )
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post(
                            endpoint, headers=headers, json=body
                        ) as response:
                            raw = await response.text()
                            logger.info(
                                "[kkt] grok images API 响应: key=%s attempt=%d status=%d bytes=%d",
                                key_label,
                                attempt + 1,
                                response.status,
                                len(raw),
                            )
                            image, err = self._handle_images_http_response(
                                response.status, raw
                            )
                            if image:
                                image = resolve_media_url(api_base, image)
                                logger.info(
                                    "[kkt] grok 图片解析成功: key=%s source=%s",
                                    key_label,
                                    "data_url" if image.startswith("data:") else "url",
                                )
                                return image
                            last_error = err or "API 响应中未找到图片"
                            raise RuntimeError(last_error)
                except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                    last_error = str(exc)
                    logger.error(
                        "[kkt] grok images API 失败: key=%s attempt=%d error=%s",
                        key_label,
                        attempt + 1,
                        last_error[:300],
                    )
                    if self._is_non_retryable_api_error(last_error):
                        raise RuntimeError(last_error) from exc
                    if self._should_switch_api_key(last_error):
                        break
                    if attempt < self.max_retry:
                        await asyncio.sleep(self.retry_delay)
                        continue
                    break
            if key_index + 1 < len(api_keys):
                logger.warning(
                    "[kkt] grok images 切换备用 Key: from=%s next_index=%d/%d last_error=%s",
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
