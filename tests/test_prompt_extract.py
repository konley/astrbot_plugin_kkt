"""Offline behavior tests for kkt prompt extraction."""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# Minimal astrbot stubs so main.py can import offline
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_stubs() -> None:
    if "aiohttp" not in sys.modules:
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

            def post(self, *a, **k):
                raise RuntimeError("offline stub")

            def get(self, *a, **k):
                raise RuntimeError("offline stub")

        class ClientError(Exception):
            pass

        aiohttp.ClientTimeout = ClientTimeout
        aiohttp.ClientSession = ClientSession
        aiohttp.ClientError = ClientError
        sys.modules["aiohttp"] = aiohttp

    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:  # pragma: no cover - interface only
        pass

    class _Filter:
        def command(self, *a, **k):
            def deco(fn):
                return fn

            return deco

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter()

    comp = types.ModuleType("astrbot.api.message_components")

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class At:
        def __init__(self, qq=None, target=None):
            self.qq = qq
            self.target = target

    class Image:
        def __init__(self, file=None, url=None):
            self.file = file
            self.url = url

        @classmethod
        def fromURL(cls, url):
            return cls(url=url)

        async def convert_to_base64(self):
            return "dGVzdA=="

    class Reply:
        def __init__(self, chain=None, id=None, **kwargs):
            self.chain = chain or []
            self.id = id if id is not None else kwargs.get("id")

    comp.Plain = Plain
    comp.At = At
    comp.Image = Image
    comp.Reply = Reply

    star = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    def register(*a, **k):
        def deco(cls):
            return cls

        return deco

    star.Context = Context
    star.Star = Star
    star.register = register

    cmd_filter = types.ModuleType("astrbot.core.star.filter.command")

    class GreedyStr(str):
        pass

    cmd_filter.GreedyStr = GreedyStr

    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_data_path = lambda: str(ROOT / ".test_data")

    core = types.ModuleType("astrbot.core")
    core_star = types.ModuleType("astrbot.core.star")
    core_filter = types.ModuleType("astrbot.core.star.filter")
    core_utils = types.ModuleType("astrbot.core.utils")

    for name, mod in [
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", event_mod),
        ("astrbot.api.message_components", comp),
        ("astrbot.api.star", star),
        ("astrbot.core", core),
        ("astrbot.core.star", core_star),
        ("astrbot.core.star.filter", core_filter),
        ("astrbot.core.star.filter.command", cmd_filter),
        ("astrbot.core.utils", core_utils),
        ("astrbot.core.utils.astrbot_path", path_mod),
    ]:
        sys.modules[name] = mod


_install_stubs()

import main as kkt  # noqa: E402
from astrbot.api import message_components as Comp  # noqa: E402


class FakeEvent:
    def __init__(self, components, message_str=""):
        self._components = components
        self._message_str = message_str

    def get_messages(self):
        return self._components

    def get_message_str(self):
        return self._message_str


Plugin = kkt.KktImagePlugin


def test_cmd_arg_regex_basic():
    assert Plugin._command_arg_from_text("/kkt 一只猫") == "一只猫"
    assert Plugin._command_arg_from_text("/hajimi 一只猫") == "一只猫"
    assert Plugin._command_arg_from_text("kkt 一只猫") == "一只猫"
    assert Plugin._command_arg_from_text("/kkt帮助") == ""
    assert Plugin._command_arg_from_text("/kkt help") == "help"
    assert Plugin._command_arg_from_text("/kkt?") == ""
    assert Plugin._command_arg_from_text("/kkt ?") == "?"


def test_strip_at_tokens():
    assert Plugin._strip_at_tokens("@牛回速归(844024041) 把帽子换成肯德基") == "把帽子换成肯德基"
    assert Plugin._strip_at_tokens("@臭包(209598428) 衣服变成泳装") == "衣服变成泳装"
    assert Plugin._strip_at_tokens("@user only") == "only"
    assert Plugin._strip_at_tokens("@only(123)") == ""


def test_extract_prompt_at_user_keeps_body():
    """核心 bug：/kkt @user xxx 必须留下 xxx。"""
    event = FakeEvent(
        components=[
            Comp.Plain("/kkt "),
            Comp.At(qq=844024041),
            Comp.Plain(" 把帽子换成肯德基"),
        ],
        message_str="/kkt @牛回速归(844024041) 把帽子换成肯德基",
    )
    # 框架 GreedyStr 只给了 @token
    result = Plugin._extract_prompt(event, "@牛回速归(844024041)")
    assert result == "把帽子换成肯德基"


def test_extract_prompt_plain_text_only():
    event = FakeEvent(
        components=[Comp.Plain("/kkt 一只穿宇航服的橘猫")],
        message_str="/kkt 一只穿宇航服的橘猫",
    )
    assert Plugin._extract_prompt(event, "一只穿宇航服的橘猫") == "一只穿宇航服的橘猫"


def test_extract_prompt_help_variants():
    for text, greedy in [
        ("/kkt help", "help"),
        ("/kkt帮助", ""),
        ("/kkt ?", "?"),
        ("/kkt", ""),
    ]:
        event = FakeEvent([Comp.Plain(text)], text)
        got = Plugin._extract_prompt(event, greedy)
        if greedy in {"help", "?"}:
            assert Plugin._is_help_token(got) or got == ""
        else:
            assert got == "" or Plugin._is_help_token(got)


def test_extract_prompt_at_only_returns_empty():
    event = FakeEvent(
        components=[Comp.Plain("/kkt "), Comp.At(qq=1)],
        message_str="/kkt @某人(1)",
    )
    assert Plugin._extract_prompt(event, "@某人(1)") == ""


def test_extract_prompt_image_plus_text_plain():
    event = FakeEvent(
        components=[
            Comp.Image(file="a.jpg"),
            Comp.Plain("/kkt 改成水彩画风"),
        ],
        message_str="/kkt 改成水彩画风",
    )
    assert Plugin._extract_prompt(event, "改成水彩画风") == "改成水彩画风"


def test_bad_old_regex_would_fail_but_new_works():
    """回归：旧 r-string 里 \\\\s 永不匹配空白。"""
    bad = re.compile(r"^/?(?:hajimi|kkt)(?:帮助|help|\\?)?\\s*(.*)$", re.I)
    good = Plugin._CMD_ARG_RE
    sample = "/kkt  把帽子换成肯德基"
    assert bad.match(sample) is None
    assert good.match(sample).group(1).strip() == "把帽子换成肯德基"


def test_parse_emoji_ids_cap_and_dedupe():
    assert Plugin._parse_emoji_ids([147, "66", 147, 0, -1, "x", 76, 124, 1, 2]) == [
        147,
        66,
        76,
        124,
        1,
    ]
    assert Plugin._parse_emoji_ids([]) == []
    assert Plugin._parse_emoji_ids("147,66") == [147, 66]
    assert Plugin._parse_emoji_ids(None) == []


def test_extract_reaction_message_id_from_message_obj():
    event = FakeEvent([], "")
    event.message_obj = SimpleNamespace(message_id=12345, raw_message=None)
    assert Plugin._extract_reaction_message_id(event) == 12345

    event.message_obj = SimpleNamespace(
        message_id=None,
        raw_message={"message_id": "999"},
    )
    assert Plugin._extract_reaction_message_id(event) == 999


def test_build_image_chain_with_quote():
    plugin = object.__new__(Plugin)
    plugin.reply_with_quote = True
    event = FakeEvent([], "")
    event.message_obj = SimpleNamespace(message_id=42, raw_message=None)
    chain = plugin._build_image_chain(event, "/tmp/a.jpg")
    assert len(chain) == 2
    assert isinstance(chain[0], Comp.Reply)
    assert chain[0].id == 42
    assert isinstance(chain[1], Comp.Image)
    assert chain[1].file == "/tmp/a.jpg"


def test_build_image_chain_without_quote():
    plugin = object.__new__(Plugin)
    plugin.reply_with_quote = False
    event = FakeEvent([], "")
    event.message_obj = SimpleNamespace(message_id=42, raw_message=None)
    chain = plugin._build_image_chain(event, "/tmp/a.jpg")
    assert len(chain) == 1
    assert isinstance(chain[0], Comp.Image)


class _UserEvent(FakeEvent):
    def __init__(self, sender_id="10001", admin=False):
        super().__init__([], "")
        self._sender_id = sender_id
        self._admin = admin
        self.message_obj = SimpleNamespace(message_id=1, raw_message=None)

    def get_sender_id(self):
        return self._sender_id

    def is_admin(self):
        return self._admin


def test_user_cooldown_blocks_same_user():
    plugin = object.__new__(Plugin)
    plugin.cooldown_seconds = 15
    plugin._user_last_call = {}
    event = _UserEvent("u1", admin=False)
    assert plugin._check_user_cooldown(event) is None
    plugin._mark_user_cooldown(event)
    msg = plugin._check_user_cooldown(event)
    assert msg is not None
    assert "秒" in msg


def test_user_cooldown_skips_admin_and_other_user():
    plugin = object.__new__(Plugin)
    plugin.cooldown_seconds = 15
    plugin._user_last_call = {}
    user = _UserEvent("u1")
    admin = _UserEvent("admin1", admin=True)
    other = _UserEvent("u2")
    plugin._mark_user_cooldown(user)
    assert plugin._check_user_cooldown(admin) is None
    assert plugin._check_user_cooldown(other) is None
    assert plugin._check_user_cooldown(user) is not None


def test_daily_quota_file_and_admin_bypass(tmp_path):
    import asyncio

    plugin = object.__new__(Plugin)
    plugin.daily_quota = 2
    plugin.quota_path = tmp_path / "daily_quota.json"
    plugin._quota_lock = asyncio.Lock()

    async def run():
        e1 = _UserEvent("u1")
        e2 = _UserEvent("u2")
        e3 = _UserEvent("u3")
        admin = _UserEvent("a1", admin=True)
        assert await plugin._check_and_consume_daily_quota(e1) is None
        assert await plugin._check_and_consume_daily_quota(e2) is None
        msg = await plugin._check_and_consume_daily_quota(e3)
        assert msg is not None and "配额" in msg
        assert await plugin._check_and_consume_daily_quota(admin) is None

    asyncio.run(run())


def test_image_label_roles():
    assert "引用" in Plugin._image_label({"source": "quote"}, 1)
    assert "当前消息" in Plugin._image_label({"source": "message"}, 2)
    assert "@小明" in Plugin._image_label({"source": "avatar", "name": "小明"}, 3)


def test_rewrite_prompt_at_and_image_num():
    items = [
        {
            "index": 1,
            "source": "avatar",
            "qq": "111",
            "name": "xx",
            "label": "图1 · @xx 的头像",
        },
        {
            "index": 2,
            "source": "avatar",
            "qq": "222",
            "name": "yy",
            "label": "图2 · @yy 的头像",
        },
    ]
    text = Plugin._rewrite_prompt_with_image_refs("让@xx给@yy洗脚", items)
    assert "图1" in text and "图2" in text
    assert "@xx" in text or "xx" in text

    text2 = Plugin._rewrite_prompt_with_image_refs(
        "让图片1手中的剑换成图片2的斧头",
        [
            {"index": 1, "source": "message"},
            {"index": 2, "source": "message"},
        ],
    )
    assert "图1" in text2 and "图2" in text2
    assert "图片1" not in text2


def test_build_multimodal_content_interleaved():
    plugin = object.__new__(Plugin)
    plugin.label_images = True
    plugin.style_prompt = ""
    items = [
        {
            "index": 1,
            "source": "quote",
            "label": "图1 · 引用原图/底图",
            "data_url": "data:image/jpeg;base64,AAA",
        },
        {
            "index": 2,
            "source": "avatar",
            "qq": "3327241564",
            "name": "北极",
            "label": "图2 · @北极 的头像",
            "data_url": "data:image/jpeg;base64,BBB",
        },
    ]
    content = plugin._build_multimodal_content("把主角换成@北极", items)
    types = [c["type"] for c in content]
    assert types.count("image_url") == 2
    assert types.count("text") >= 3
    # 标签文本出现在对应图片之前
    first_img = next(i for i, c in enumerate(content) if c["type"] == "image_url")
    assert content[first_img - 1]["type"] == "text"
    assert "图1" in content[first_img - 1]["text"]
    joined = " ".join(c["text"] for c in content if c["type"] == "text")
    assert "用户指令" in joined
    assert "图2" in joined


def test_extract_text_reply_from_chat_completion():
    data = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "抱歉，我无法生成该内容。",
                },
            }
        ]
    }
    assert Plugin._extract_image(data) is None
    assert "无法生成" in Plugin._extract_text_reply(data)
    assert Plugin._extract_finish_reason(data) == "stop"


def test_extract_image_from_images_field_and_b64():
    data = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "images": [
                        {"url": "data:image/png;base64,AAA"},
                    ],
                }
            }
        ]
    }
    assert Plugin._extract_image(data) == "data:image/png;base64,AAA"

    data2 = {
        "choices": [
            {
                "message": {
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://cdn.example.com/a.png"},
                        }
                    ]
                }
            }
        ]
    }
    assert "a.png" in (Plugin._extract_image(data2) or "")


def test_compose_user_instruction_chinese_style():
    plugin = object.__new__(Plugin)
    plugin.style_prompt = "【画面文字语言】默认简体中文"
    text = plugin._compose_user_instruction("画一只猫")
    assert "简体中文" in text
    assert "画一只猫" in text
    assert "用户指令" in text

    plugin.style_prompt = ""
    text2 = plugin._compose_user_instruction("画一只猫")
    assert text2.startswith("用户指令") or "画一只猫" in text2


def test_build_content_includes_style_prompt():
    plugin = object.__new__(Plugin)
    plugin.label_images = False
    plugin.style_prompt = "【画面文字语言】默认简体中文"
    content = plugin._build_multimodal_content("生成四格漫画", [])
    assert any(
        c.get("type") == "text" and "简体中文" in c.get("text", "") for c in content
    )
