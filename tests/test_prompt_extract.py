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
        def __init__(self, chain=None):
            self.chain = chain or []

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
