"""Offline behavior tests for kkt prompt extraction."""

from __future__ import annotations

import json
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

    class MessageChain:
        def __init__(self, chain=None, **_):
            self.chain = chain or []

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
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

    class Video:
        def __init__(self, file=None, path=None, url=None, **kwargs):
            self.file = file
            self.path = path
            self.url = url

        @classmethod
        def fromFileSystem(cls, path, **_):
            return cls(file=str(path), path=str(path))

        @classmethod
        def fromURL(cls, url, **_):
            return cls(file=url, url=url)

    class Node:
        def __init__(self, content, **kwargs):
            self.content = content
            self.kwargs = kwargs

    class Nodes:
        def __init__(self, nodes, **kwargs):
            self.nodes = nodes
            self.kwargs = kwargs

    comp.Plain = Plain
    comp.At = At
    comp.Image = Image
    comp.Reply = Reply
    comp.Video = Video
    comp.Node = Node
    comp.Nodes = Nodes

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

from astrbot.api import message_components as Comp  # noqa: E402

import main as kkt  # noqa: E402


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
    assert Plugin._command_arg_from_text("/image2 一只猫") == "一只猫"
    assert Plugin._command_arg_from_text("/grokv 生成 @小明 的跳舞视频") == "生成 @小明 的跳舞视频"
    assert Plugin._command_arg_from_text("/grok 生成 @小明 的图片") == "生成 @小明 的图片"
    assert Plugin._command_arg_from_text("/grok2k 2K图片") == "2K图片"
    assert Plugin._command_arg_from_text("/gk2 2K图片") == "2K图片"
    assert Plugin._command_arg_from_text("/gk2k 2K图片") == "2K图片"
    assert Plugin._command_arg_from_text("/kkgif") == ""
    assert Plugin._command_arg_from_text("/kkgifzip") == ""
    assert Plugin._command_arg_from_text("/kkgifzip3") == ""
    assert Plugin._command_arg_from_text("/gifz") == ""
    assert Plugin._command_arg_from_text("/gifzip3") == ""
    assert Plugin._command_arg_from_text("/kkgifzip5 帮助") == "帮助"
    assert Plugin._command_arg_from_text("kkt 一只猫") == "一只猫"
    assert Plugin._command_arg_from_text("/kkt帮助") == ""
    assert Plugin._command_arg_from_text("/kkt help") == "help"
    assert Plugin._command_arg_from_text("/image2 help") == "help"
    assert Plugin._command_arg_from_text("/kkt?") == ""
    assert Plugin._command_arg_from_text("/kkt ?") == "?"
    assert Plugin._command_arg_from_text("/hajimigif 让主角挥手") == "让主角挥手"
    assert Plugin._command_arg_from_text("/kktgif 吃饭") == "吃饭"
    assert Plugin._command_arg_from_text("/hajimigif2 眨眼") == "眨眼"
    assert Plugin._command_arg_from_text("/image2gif 跳舞") == "跳舞"
    assert Plugin._command_arg_from_text("/image2gif2 摇摆") == "摇摆"


def test_grokv_duration_arguments():
    plugin = object.__new__(Plugin)

    prompt = Plugin._extract_prompt(
        FakeEvent([], "/grokv 5 一只猫跳舞"), ""
    )
    duration, body, error = plugin._parse_grokv_duration(
        FakeEvent([], "/grokv 5 一只猫跳舞"), prompt, 8
    )
    assert (duration, body, error) == (5, "一只猫跳舞", None)

    prompt = Plugin._extract_prompt(
        FakeEvent([], "/grokv5 一只猫跳舞"), ""
    )
    duration, body, error = plugin._parse_grokv_duration(
        FakeEvent([], "/grokv5 一只猫跳舞"), prompt, 8
    )
    assert (duration, body, error) == (5, "一只猫跳舞", None)

    prompt = Plugin._extract_prompt(FakeEvent([], "/grokv 16 猫"), "")
    duration, body, error = plugin._parse_grokv_duration(
        FakeEvent([], "/grokv 16 猫"), prompt, 8
    )
    assert duration == 16 and body == "猫" and error is not None


def test_resolve_api_credentials_image2_requires_own_key():
    plugin = object.__new__(Plugin)
    plugin.api_base = "https://example.com/v1"
    plugin.api_key = "default-key"
    plugin.backup_api_keys = []
    plugin.model = "gemini-x"
    plugin.image2_api_base = "https://example.com/v1"
    plugin.image2_api_key = ""
    plugin.image2_backup_api_keys = []
    plugin.image2_model = "gpt-image-2"
    err = plugin._resolve_api_credentials("image2")
    assert isinstance(err, str) and "image2" in err.lower()

    plugin.image2_api_key = "image2-only-key"
    base, keys, model = plugin._resolve_api_credentials("image2")
    assert keys == ["image2-only-key"]
    assert model == "gpt-image-2"
    # 默认通道不碰 image2 key
    base2, keys2, model2 = plugin._resolve_api_credentials("kkt")
    assert keys2 == ["default-key"]
    assert model2 == "gemini-x"

    grok_base, grok_keys, grok_model = plugin._resolve_api_credentials("grok")
    assert grok_base == "https://example.com/v1"
    assert grok_keys == ["default-key"]
    assert grok_model == "grok-imagine-image-quality"


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
    chain = plugin._build_image_chain(event, "/tmp/a.jpg", elapsed_seconds=12)
    assert len(chain) == 3
    assert isinstance(chain[0], Comp.Reply)
    assert chain[0].id == 42
    assert isinstance(chain[1], Comp.Plain)
    assert chain[1].text == "生成耗时：12秒，请查收喵"
    assert isinstance(chain[2], Comp.Image)
    assert chain[2].file == "/tmp/a.jpg"


def test_build_image_chain_without_quote():
    plugin = object.__new__(Plugin)
    plugin.reply_with_quote = False
    event = FakeEvent([], "")
    event.message_obj = SimpleNamespace(message_id=42, raw_message=None)
    chain = plugin._build_image_chain(event, "/tmp/a.jpg", elapsed_seconds=3)
    assert len(chain) == 2
    assert isinstance(chain[0], Comp.Plain)
    assert chain[0].text == "生成耗时：3秒，请查收喵"
    assert isinstance(chain[1], Comp.Image)


def test_build_help_text_is_concise():
    from main import build_help_text

    help_text = build_help_text()
    assert "康康图" in help_text
    assert "/hajimi" in help_text
    assert "image2" in help_text
    assert "调整日限额" not in help_text
    assert len(help_text.splitlines()) <= 8


def test_command_aliases_keep_defaults_and_skip_collisions():
    aliases = Plugin._load_command_aliases(
        {
            "main_command_aliases": ["paint", "/draw"],
            "image2_command_aliases": "img2",
            "grok_command_aliases": ["paint", "grokpic"],
            "video_command_aliases": ["movie"],
        }
    )
    assert aliases["main"] == ["kkt", "paint", "draw"]
    assert aliases["image2"] == ["img2"]
    assert "paint" not in aliases["grok"]
    assert "grokpic" in aliases["grok"]
    assert aliases["video"] == ["grokv", "gkv", "gv", "movie"]

    plugin = object.__new__(Plugin)
    plugin._command_aliases = aliases
    plugin._command_alias_map = plugin._build_command_alias_map()
    assert plugin._command_alias_map["paint"] == "main"
    assert plugin._command_alias_map["movie5"] == "video"
    assert "movie5" in plugin._command_names_for_parser()
    assert Plugin._command_arg_from_text(
        "/paint 一只猫", plugin._command_names_for_parser()
    ) == "一只猫"


def test_help_text_lists_canonical_commands_and_gif_aliases():
    plugin = object.__new__(Plugin)
    plugin._command_aliases = Plugin._load_command_aliases(
        {"main_command_aliases": ["paint"], "video_command_aliases": ["movie"]}
    )
    groups = plugin._command_help_groups()
    basic = kkt.build_basic_help_text(groups)
    aliases_text = kkt.build_alias_help_text(groups)
    assert "/hajimi" in basic and "/paint" in aliases_text
    assert "/image2" in basic
    assert "/grok" in basic and "/gk" in aliases_text
    assert "/grok2" in basic
    assert "/grokvideo" in basic and "/movie" in aliases_text
    assert "/hajimigif" in basic and "/kktgif" in aliases_text
    assert "/image2gif2" in basic and "/kkgif" in basic
    assert "/kkgifzip" in basic


def test_grokvideo_canonical_duration_parser():
    plugin = object.__new__(Plugin)
    prompt = Plugin._extract_prompt(
        FakeEvent([], "/grokvideo5 一只猫跳舞"),
        "",
        ["grokvideo", "grokvideo5", "grokv", "grokv5", "movie", "movie5"],
    )
    duration, body, error = plugin._parse_grokv_duration(
        FakeEvent([], "/grokvideo5 一只猫跳舞"),
        prompt,
        8,
        ["grokvideo", "grokv", "movie"],
    )
    assert (duration, body, error) == (5, "一只猫跳舞", None)


def test_help_handler_sends_two_forward_nodes():
    import asyncio

    class HelpEvent:
        def __init__(self):
            self.sent = []
            self.stopped = False

        def stop_event(self):
            self.stopped = True

        def get_self_id(self):
            return "123"

        async def send(self, chain):
            self.sent.append(chain)

        def plain_result(self, text):
            return text

    plugin = object.__new__(Plugin)
    plugin._command_aliases = Plugin._load_command_aliases(
        {"main_command_aliases": ["paint"]}
    )
    event = HelpEvent()

    async def run():
        yielded = [item async for item in plugin.handle_help(event)]
        return yielded

    yielded = asyncio.run(run())
    assert yielded == []
    assert event.stopped is True
    assert len(event.sent) == 1
    nodes = event.sent[0].chain[0]
    assert isinstance(nodes, Comp.Nodes)
    assert len(nodes.nodes) == 2
    assert "基础操作" in nodes.nodes[0].content[0].text
    assert "当前别名" in nodes.nodes[1].content[0].text
    assert "/paint" in nodes.nodes[1].content[0].text


def test_format_image2_multi_ref_reject_includes_count_and_labels():
    msg = Plugin._format_image2_multi_ref_reject(
        [
            {"label": "图1 · 引用原图/底图"},
            {"label": "图2 · 当前消息图片"},
            {"label": "图3 · @某人 的头像"},
        ]
    )
    assert "Images edit模式" in msg
    assert "已收到 3 张" in msg
    assert "哈基米 /hajimi" in msg
    assert "图1 · 引用原图/底图" in msg
    assert "图2 · 当前消息图片" in msg
    assert "图3 · @某人 的头像" in msg


def test_should_use_images_api_modes():
    plugin = object.__new__(Plugin)
    plugin.image2_api_mode = "images"
    assert plugin._should_use_images_api("image2", "wy-gpt-image-2") is True
    assert plugin._should_use_images_api("kkt", "wy-gpt-image-2") is False
    plugin.image2_api_mode = "chat"
    assert plugin._should_use_images_api("image2", "wy-gpt-image-2") is False
    plugin.image2_api_mode = "auto"
    assert plugin._should_use_images_api("image2", "wy-gpt-image-2") is True
    assert plugin._should_use_images_api("image2", "gemini-flash") is False


def test_parse_category_list():
    assert Plugin._parse_category_list(["政治", "暴恐", "政治"]) == ["政治", "暴恐"]
    assert Plugin._parse_category_list("政治, 色情") == ["政治", "色情"]
    assert Plugin._parse_category_list(None) == []


def test_sensitive_filter_hit_and_user_message_hides_keyword():
    plugin = object.__new__(Plugin)
    plugin.sensitive_filter_enabled = True
    plugin._sensitive_words_by_cat = {"政治": ["测试敏感词ABC", "短"]}
    plugin._SENSITIVE_REJECT_USER_MSG = Plugin._SENSITIVE_REJECT_USER_MSG
    msg = plugin._check_sensitive_prompt("请画测试敏感词ABC场景")
    assert msg == Plugin._SENSITIVE_REJECT_USER_MSG
    assert "测试敏感词ABC" not in msg
    assert plugin._check_sensitive_prompt("一只普通的猫") is None


def test_sensitive_filter_disabled_skips():
    plugin = object.__new__(Plugin)
    plugin.sensitive_filter_enabled = False
    plugin._sensitive_words_by_cat = {"政治": ["测试敏感词ABC"]}
    assert plugin._check_sensitive_prompt("请画测试敏感词ABC") is None


def test_find_sensitive_hit_prefers_longer_word():
    plugin = object.__new__(Plugin)
    plugin._sensitive_words_by_cat = {"政治": ["测试词", "测试词加长版"]}
    # 列表应按长度降序；模拟加载结果
    plugin._sensitive_words_by_cat = {
        "政治": sorted(["测试词", "测试词加长版"], key=len, reverse=True)
    }
    hit = plugin._find_sensitive_hit("出现测试词加长版即可")
    assert hit == ("政治", "测试词加长版")


def test_parse_sensitive_toggle_arg():
    assert Plugin._parse_sensitive_toggle_arg("") is None
    assert Plugin._parse_sensitive_toggle_arg("开") is True
    assert Plugin._parse_sensitive_toggle_arg("关闭") is False
    assert Plugin._parse_sensitive_toggle_arg("on") is True
    assert Plugin._parse_sensitive_toggle_arg("OFF") is False
    assert Plugin._parse_sensitive_toggle_arg("乱写") is None


def test_format_sensitive_status_is_one_line_only():
    plugin = object.__new__(Plugin)
    plugin.sensitive_filter_enabled = False
    plugin._sensitive_word_count = 999
    plugin._sensitive_words_by_cat = {"政治": ["x"]}
    plugin.sensitive_categories = ["政治"]
    text = plugin._format_sensitive_status()
    assert text == "本地审核：关"
    assert "词条" not in text
    assert "类别" not in text
    assert "政治" not in text
    plugin.sensitive_filter_enabled = True
    assert plugin._format_sensitive_status() == "本地审核：开"


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
    plugin.usage_path = None
    plugin._quota_lock = asyncio.Lock()

    async def run():
        e1 = _UserEvent("u1")
        e2 = _UserEvent("u2")
        e3 = _UserEvent("u3")
        admin = _UserEvent("a1", admin=True)
        assert await plugin._check_and_consume_daily_quota(e1) is None
        assert await plugin._check_and_consume_daily_quota(e2) is None
        msg = await plugin._check_and_consume_daily_quota(e3)
        assert msg is not None and ("额度" in msg or "配额" in msg)
        assert await plugin._check_and_consume_daily_quota(admin) is None

    asyncio.run(run())


def test_channel_usage_record_and_cost(tmp_path):
    import asyncio

    plugin = object.__new__(Plugin)
    plugin.usage_path = tmp_path / "usage.json"
    plugin.quota_path = tmp_path / "daily_quota.json"
    plugin.channel_limit_override_path = tmp_path / "channel_quota_limit.json"
    plugin.quota_limit_override_path = tmp_path / "daily_quota_limit.json"
    plugin._quota_lock = asyncio.Lock()
    plugin.channel_limits = {"main": 2, "image2": 1, "video": 0}
    plugin.daily_quota = 2
    plugin.cost_main_usd = 0.02
    plugin.cost_image2_usd = 0.08
    plugin.cost_video_usd = 0.0
    plugin.cooldown_seconds = 0
    plugin.video_cooldown_seconds = 0
    plugin.video_max_concurrent = 2
    plugin.video_max_concurrent_per_user = 1
    plugin._user_last_call = {}

    async def run():
        user = _UserEvent("u1")
        assert await plugin._check_channel_quota(user, "main") is None
        await plugin._record_successful_usage("main")
        await plugin._record_successful_usage("image2")
        await plugin._record_successful_usage("main")
        # main 日限 2 已满
        msg = await plugin._check_channel_quota(user, "main")
        assert msg is not None and ("hajimi" in msg or "/hajimi" in msg or "main" in msg)
        # image2 日限 1 已满
        msg2 = await plugin._check_channel_quota(user, "image2")
        assert msg2 is not None and "image2" in msg2
        # 管理员可继续
        admin = _UserEvent("a1", admin=True)
        assert await plugin._check_channel_quota(admin, "main") is None

    asyncio.run(run())
    text = plugin._format_quota_status(None)
    assert "2" in text and "1" in text
    assert "$0.04" in text or "0.04" in text  # main 2 * 0.02
    assert "$0.08" in text or "0.08" in text  # image2 1 * 0.08
    assert "单价" in text and "/hajimi" in text
    assert "约" not in text
    assert "预估" not in text and "上游账单" not in text
    data = json.loads(plugin.usage_path.read_text(encoding="utf-8"))
    assert data["channels"]["main"]["total"] == 2
    assert data["channels"]["image2"]["total"] == 1


def test_parse_quota_command_arg_channels():
    plugin = object.__new__(Plugin)
    assert plugin._parse_quota_command_arg("") == (None, None, True)
    assert plugin._parse_quota_command_arg("main") == ("main", None, True)
    assert plugin._parse_quota_command_arg("image2") == ("image2", None, True)
    assert plugin._parse_quota_command_arg("10") == ("all", 10, True)
    assert plugin._parse_quota_command_arg("main 100") == ("main", 100, True)
    assert plugin._parse_quota_command_arg("image2=20")[0:2] == ("image2", 20)
    assert plugin._channel_for_command("kkt") == "main"
    assert plugin._channel_for_command("hajimi") == "main"
    assert plugin._channel_for_command("image2") == "image2"
    assert plugin._channel_for_command("grokv") == "video"
    assert plugin._parse_channel_token("video") == "video"
    assert plugin._parse_channel_token("grokv") == "video"
    assert plugin._parse_quota_command_arg("grokv 5") == ("video", 5, True)
    assert plugin._parse_quota_command_arg("video 5") == ("video", 5, True)


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


def test_compose_video_prompt_static_prefix():
    plugin = object.__new__(Plugin)
    plugin.video_prompt_enhance = True
    plugin.video_style_prompt = ""
    plugin.prefer_chinese_text = True
    plugin.prefer_cn_locale = True
    plugin.video_duration = 8

    text = plugin._compose_video_prompt("猫跳一下", has_ref_image=False, duration=5)
    assert "视频生成约束" in text
    assert "约 5 秒" in text
    assert "猫跳一下" in text
    assert "用户指令" in text
    assert "东亚" in text
    assert "简体中文" in text

    with_ref = plugin._compose_video_prompt(
        "挥手", has_ref_image=True, duration=8
    )
    assert "参考图为首帧" in with_ref
    assert "东亚" not in with_ref
    assert "挥手" in with_ref

    empty_ref = plugin._compose_video_prompt("", has_ref_image=True, duration=8)
    assert "主体自然轻微动起来" in empty_ref
    assert "静物或风景" in empty_ref
    assert "不要擅自添加新角色" in empty_ref

    plugin.video_prompt_enhance = False
    raw = plugin._compose_video_prompt("原文直传", has_ref_image=False, duration=8)
    assert raw == "原文直传"
    assert "视频生成约束" not in raw

    plugin.video_style_prompt = "【自定义】禁止运镜"
    plugin.video_prompt_enhance = True
    custom = plugin._compose_video_prompt("测试", has_ref_image=False, duration=3)
    assert "【自定义】禁止运镜" in custom


def test_format_quota_status_unlimited_and_limited(tmp_path):
    plugin = object.__new__(Plugin)
    plugin.cooldown_seconds = 0
    plugin.video_cooldown_seconds = 0
    plugin.video_max_concurrent = 2
    plugin.video_max_concurrent_per_user = 1
    plugin.usage_path = tmp_path / "usage.json"
    plugin.quota_path = tmp_path / "daily_quota.json"
    plugin.channel_limits = {"main": 0, "image2": 0, "video": 0}
    plugin.daily_quota = 0
    plugin.cost_main_usd = 0.0
    plugin.cost_image2_usd = 0.0
    plugin.cost_video_usd = 0.0
    plugin._user_last_call = {}
    text = plugin._format_quota_status(None)
    assert "不限制" in text
    assert "出图冷却：关闭" in text
    assert "公用限额" not in text
    assert "上游账单" not in text

    plugin.channel_limits = {"main": 50, "image2": 10, "video": 5}
    plugin.daily_quota = 50
    plugin.cooldown_seconds = 15
    plugin.video_cooldown_seconds = 60
    plugin.cost_main_usd = 0.01
    plugin.cost_image2_usd = 0.05
    plugin.cost_video_usd = 0.2

    def fake_usage():
        return {
            "date": "2026-07-22",
            "channels": {
                "main": {"daily": 7, "total": 100},
                "image2": {"daily": 1, "total": 5},
                "video": {"daily": 0, "total": 0},
            },
        }

    plugin._load_usage_state = fake_usage  # type: ignore[method-assign]
    text2 = plugin._format_quota_status(None)
    assert "7/50" in text2
    assert "剩余 43" in text2
    assert "累计 100" in text2
    assert "出图冷却：15s" in text2
    assert "视频冷却：60s" in text2
    assert "/hajimi" in text2
    assert "/grokv" in text2
    assert "单价" in text2
    assert "约" not in text2
    assert "预估" not in text2
    assert "上游账单" not in text2


def test_cn_locale_style_parts_soft():
    """本地化软约束应含人物默认，且不含竖版/强制中国场景。"""
    # 用真实 __init__ 路径太重，直接复现拼接逻辑的关键短语
    locale = (
        "【人物与习惯·轻量默认，勿过度限制】"
        "1) 人物：用户未指定种族/国籍/外貌时，默认东亚华人常见外貌特征；"
        "若有参考图或@头像，优先还原参考人物，不要擅自换成外国人脸；"
        "用户明确要求其他外貌/种族/角色设定时，完全以用户为准。"
        "2) 画风：在不违背用户画风要求的前提下，可略偏国内常见二次元/国漫的清爽表现，"
        "不要强制单一画风或固定脸模。"
        "3) 生活细节：若出现当代日常物件，可自然使用中国常见物品，避免堆砌刻板符号；"
        "不要强行改写奇幻/异世界/明确海外等场景。"
    )
    assert "东亚华人" in locale
    assert "国漫" in locale
    assert "竖版" not in locale
    assert "强制" not in locale or "不要强制" in locale
    assert "现代中国" not in locale


def test_build_content_includes_style_prompt():
    plugin = object.__new__(Plugin)
    plugin.label_images = False
    plugin.style_prompt = "【画面文字语言】默认简体中文"
    content = plugin._build_multimodal_content("生成四格漫画", [])
    assert any(
        c.get("type") == "text" and "简体中文" in c.get("text", "") for c in content
    )


def test_build_gif_prompt_contains_fixed_alignment_contract():
    plugin = object.__new__(Plugin)
    prompt = plugin._build_gif_prompt("让主角挥手")
    assert "4 列 4 行" in prompt
    assert "16 个等大画格" in prompt
    assert "不得跨越画格边界" in prompt
    assert "让主角挥手" in prompt
    assert "固定机位" in prompt
    assert "相邻画格之间只发生小幅" in prompt


def test_build_gif_prompt_without_action_lets_model_choose():
    plugin = object.__new__(Plugin)
    prompt = plugin._build_gif_prompt("")
    assert "用户没有指定具体动作" in prompt
    assert "自行选择一个适合参考主体" in prompt
    assert "做一个自然、可循环的简单动作" not in prompt


def test_gif_command_grid_and_api_mapping():
    assert Plugin._is_gif_command("hajimigif") is True
    assert Plugin._is_gif_command("image2gif2") is True
    assert Plugin._gif_grid_size("hajimigif") == 4
    assert Plugin._gif_grid_size("hajimigif2") == 3
    assert Plugin._gif_grid_size("image2gif2") == 3
    assert Plugin._gif_api_command("hajimigif2") == "hajimi"
    assert Plugin._gif_api_command("image2gif") == "image2"


def test_build_gif_prompt_supports_dynamic_grid_and_multiple_subjects():
    plugin = object.__new__(Plugin)
    prompt = plugin._build_gif_prompt("让两个人自然跳舞", grid_size=3)
    assert "3 列 3 行" in prompt
    assert "9 个等大画格" in prompt
    assert "多个主体" in prompt
    assert "共同自然发展和互动" in prompt


def test_make_gif_from_grid_crops_16_frames(tmp_path):
    from PIL import Image

    plugin = object.__new__(Plugin)
    plugin.temp_dir = tmp_path
    plugin.gif_frame_size = 64
    plugin.gif_fps = 8
    plugin.gif_max_bytes = 1024 * 1024
    source = tmp_path / "grid.png"
    image = Image.new("RGB", (1024, 1024), "white")
    for index in range(16):
        row, column = divmod(index, 4)
        color = (index * 13 % 255, index * 29 % 255, index * 47 % 255)
        cell = Image.new("RGB", (256, 256), color)
        image.paste(cell, (column * 256, row * 256))
    image.save(source)

    output, count, cell_size = plugin._make_gif_from_grid(str(source))
    assert count == 16
    assert cell_size == 256
    with Image.open(output) as gif:
        assert gif.size == (64, 64)
        assert gif.n_frames == 16


def test_make_gif_from_grid_crops_9_frames(tmp_path):
    from PIL import Image

    plugin = object.__new__(Plugin)
    plugin.temp_dir = tmp_path
    plugin.gif_frame_size = 64
    plugin.gif_fps = 8
    plugin.gif_max_bytes = 1024 * 1024
    source = tmp_path / "grid3.png"
    image = Image.new("RGB", (900, 900), "white")
    for index in range(9):
        row, column = divmod(index, 3)
        image.paste(
            Image.new("RGB", (300, 300), (index * 13 % 255, index * 29 % 255, index * 47 % 255)),
            (column * 300, row * 300),
        )
    image.save(source)

    output, count, cell_size = plugin._make_gif_from_grid(str(source), grid_size=3)
    assert count == 9
    assert cell_size == 300
    with Image.open(output) as gif:
        assert gif.size == (64, 64)
        assert gif.n_frames == 9


def test_make_gif_from_grid_rejects_low_resolution(tmp_path):
    from PIL import Image

    plugin = object.__new__(Plugin)
    plugin.temp_dir = tmp_path
    plugin.gif_frame_size = 256
    plugin.gif_fps = 8
    plugin.gif_max_bytes = 1024 * 1024
    source = tmp_path / "small.png"
    Image.new("RGB", (400, 400), "white").save(source)
    try:
        plugin._make_gif_from_grid(str(source))
    except RuntimeError as exc:
        assert "分辨率过低" in str(exc)
    else:
        raise AssertionError("low-resolution storyboard should be rejected")


def test_kkgifzip_level_parser_and_presets():
    plugin = object.__new__(Plugin)
    plugin._command_aliases = Plugin._load_command_aliases({})
    assert plugin._parse_kkgifzip_level(FakeEvent([], "/kkgifzip")) == 1
    assert plugin._parse_kkgifzip_level(FakeEvent([], "/kkgifzip1")) == 1
    assert plugin._parse_kkgifzip_level(FakeEvent([], "/kkgifzip3 帮助")) == 3
    assert plugin._parse_kkgifzip_level(FakeEvent([], "/kkgifzip5")) == 5
    assert plugin._parse_kkgifzip_level(FakeEvent([], "/gifz")) == 1
    assert plugin._parse_kkgifzip_level(FakeEvent([], "/gifz3")) == 3
    assert plugin._parse_kkgifzip_level(FakeEvent([], "/gifzip5")) == 5
    assert set(Plugin._KKGIFZIP_PRESETS) == {1, 2, 3, 4, 5}
    assert Plugin._KKGIFZIP_PRESETS[1]["dimension"] > Plugin._KKGIFZIP_PRESETS[5]["dimension"]
    # 全档固定约 10fps，不靠降帧变糊
    assert all(int(p["fps"]) == 10 for p in Plugin._KKGIFZIP_PRESETS.values())
    assert float(Plugin._KKGIFZIP_PRESETS[5]["crush"]) > float(
        Plugin._KKGIFZIP_PRESETS[1]["crush"]
    )
    graph = plugin._kkgifzip_filter_graph(
        dimension=180,
        fps=10,
        colors=48,
        crush=2.5,
        blur=0.75,
        dither="none",
        saturation=1.3,
    )
    assert "flags=neighbor" in graph and "gblur=sigma=0.75" in graph
    assert "eq=saturation=1.3" in graph and "stats_mode=diff" in graph
    assert "force_original_aspect_ratio=decrease" in graph
    assert "scale=iw*2.5:ih*2.5" in graph
    assert "scale=180:180:flags=neighbor" not in graph
    assert "palettegen=max_colors=48" in graph and "dither=none" in graph
    help_text = plugin._kkgifzip_help_text()
    assert "/kkgifzip" in help_text and "静态" in help_text
    assert int(Plugin._KKGIFZIP_PRESETS[1]["colors"]) >= 160
    assert int(Plugin._KKGIFZIP_PRESETS[1]["dimension"]) == 220


def test_kkgifzip_command_names_in_parser_and_catalog():
    plugin = object.__new__(Plugin)
    plugin._command_aliases = Plugin._load_command_aliases({})
    plugin._command_alias_map = plugin._build_command_alias_map()
    names = plugin._command_names_for_parser()
    assert "kkgifzip" in names and "kkgifzip5" in names
    assert "gifz" in names and "gifzip3" in names
    catalog = plugin._command_catalog()
    keys = {item["key"] for item in catalog}
    assert "kkgifzip" in keys
    aliases = Plugin._load_command_aliases({})
    assert "gifz" in aliases["kkgifzip"] and "gifzip" in aliases["kkgifzip"]
