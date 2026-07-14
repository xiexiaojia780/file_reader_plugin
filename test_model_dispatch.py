"""mock 测试 host / external 分派、NapCat 兜底、路径白名单与错误收敛。

不依赖真实 maibot_sdk / 外部网络，验证 handler 与内部辅助逻辑。
"""

from __future__ import annotations

import asyncio
import base64
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


# ---------- 准备假的 maibot_sdk 模块（避免真装 SDK） ----------
def _install_fake_sdk() -> None:
    if "maibot_sdk" in sys.modules:
        return

    fake = types.ModuleType("maibot_sdk")

    class PluginConfigBase:
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def Field(default: Any = None, default_factory: Any = None, **_: Any) -> Any:
        if default_factory is not None:
            return default_factory()
        return default

    class MaiBotPlugin:
        config_model: Any = None

        def __init__(self) -> None:
            self.ctx: Any = None
            self.config: Any = None

    def Command(*_a: Any, **_kw: Any):
        def deco(fn):
            return fn

        return deco

    fake.PluginConfigBase = PluginConfigBase
    fake.Field = Field
    fake.MaiBotPlugin = MaiBotPlugin
    fake.Command = Command
    sys.modules["maibot_sdk"] = fake


_install_fake_sdk()
sys.path.insert(0, str(Path(__file__).parent))

from plugin import (  # noqa: E402
    FileReaderPlugin,
    FileReaderPluginConfig,
    ModelConfig,
    NapcatConfig,
    ReadConfig,
)


def _make_plugin(
    model_config: ModelConfig | None = None,
    *,
    napcat: NapcatConfig | None = None,
    read: ReadConfig | None = None,
    access: Any = None,
    enabled: bool = True,
) -> FileReaderPlugin:
    plugin = FileReaderPlugin()
    cfg = FileReaderPluginConfig()
    cfg.plugin.enabled = enabled
    if model_config is not None:
        cfg.model = model_config
    if napcat is not None:
        cfg.napcat = napcat
    if read is not None:
        cfg.read = read
    if access is not None:
        cfg.access = access
    plugin.config = cfg

    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.send.text = AsyncMock()
    ctx.llm.generate = AsyncMock(
        return_value={"success": True, "response": "HOST_REPLY", "reasoning": "", "model": "replyer"}
    )
    ctx.llm.generate_with_tools = AsyncMock(
        return_value={"success": True, "response": "HOST_REPLY", "reasoning": "", "model": "replyer", "tool_calls": []}
    )
    plugin.ctx = ctx
    return plugin


def _assert_reply_not_stored(plugin: FileReaderPlugin) -> None:
    """命令回复必须关闭入库，避免 Maisaka 再接话。"""

    assert plugin.ctx.send.text.await_count >= 1
    for call in plugin.ctx.send.text.await_args_list:
        kwargs = call.kwargs
        assert kwargs.get("storage_message") is False, f"应 storage_message=False，得到 {kwargs}"
        assert kwargs.get("sync_to_maisaka_history") is False, f"应 sync_to_maisaka_history=False，得到 {kwargs}"


def _host_model(**kwargs: Any) -> ModelConfig:
    defaults = dict(
        mode="host",
        task_name="replyer",
        provider="openai",
        api_base_url="x",
        api_key="x",
        model_name="x",
        temperature=0.7,
        max_tokens=2048,
    )
    defaults.update(kwargs)
    return ModelConfig(**defaults)


def _external_model(**kwargs: Any) -> ModelConfig:
    defaults = dict(
        mode="external",
        task_name="",
        provider="openai",
        api_base_url="https://example.com/v1/",
        api_key="sk-test",
        model_name="gpt-4o-mini",
        temperature=0.5,
        max_tokens=512,
    )
    defaults.update(kwargs)
    return ModelConfig(**defaults)


async def _run_handler(
    plugin: FileReaderPlugin,
    *,
    body: str = "abc 总结",
    message: dict[str, Any] | None = None,
    platform: str = "qq",
    user_id: str = "10001",
) -> tuple[bool, str, int]:
    return await plugin.handle_read_file(
        stream_id="s1",
        matched_groups={"body": body},
        message=message or {},
        platform=platform,
        user_id=user_id,
    )


def _mock_aiohttp_session(payload: dict[str, Any] | None = None, *, raise_status: bool = False) -> MagicMock:
    fake_resp = MagicMock()
    if raise_status:
        fake_resp.raise_for_status = MagicMock(side_effect=RuntimeError("http 500"))
    else:
        fake_resp.raise_for_status = MagicMock()
    fake_resp.json = AsyncMock(return_value=payload if payload is not None else {})
    fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_resp.__aexit__ = AsyncMock(return_value=None)

    fake_session = MagicMock()
    fake_session.post = MagicMock(return_value=fake_resp)
    fake_session.get = MagicMock(return_value=fake_resp)
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    return fake_session


# ---------- 模型分派 ----------


async def test_host_mode() -> None:
    print("[host] 启动")
    plugin = _make_plugin(_host_model(task_name="replyer"))
    plugin._fetch_file_bytes = AsyncMock(return_value=(b"hello world", "test"))
    plugin._decode_text = MagicMock(return_value=("hello world content", False))
    ok, msg, intercept = await _run_handler(plugin)
    assert ok, f"host 应该成功，得到 {msg}"
    assert intercept >= 1, f"应拦截后续主链，得到 intercept={intercept}"
    # 默认启用固定 tools，应走 generate_with_tools
    plugin.ctx.llm.generate_with_tools.assert_awaited_once()
    plugin.ctx.llm.generate.assert_not_awaited()
    call_kwargs = plugin.ctx.llm.generate_with_tools.await_args.kwargs
    assert call_kwargs.get("model") == "replyer", f"host 应传 task_name=replyer，得到 {call_kwargs}"
    tools = call_kwargs.get("tools") or []
    assert isinstance(tools, list) and tools, "应附带固定 tools"
    assert tools[0].get("name") == "submit_file_result"
    sent_texts = [c.args[0] for c in plugin.ctx.send.text.await_args_list if c.args]
    assert "HOST_REPLY" in sent_texts
    _assert_reply_not_stored(plugin)
    print(f"[host] OK：generate_with_tools(model={call_kwargs.get('model')!r}) tools={len(tools)} intercept={intercept}")


async def test_host_mode_empty_task() -> None:
    print("[host-empty] 启动")
    plugin = _make_plugin(_host_model(task_name="   "))
    plugin._fetch_file_bytes = AsyncMock(return_value=(b"hello world", "test"))
    plugin._decode_text = MagicMock(return_value=("hello world content", False))
    await _run_handler(plugin)
    call_kwargs = plugin.ctx.llm.generate_with_tools.await_args.kwargs
    assert call_kwargs.get("model") is None, f"空 task_name 应传 None，得到 {call_kwargs}"
    print("[host-empty] OK：空 task_name -> model=None")


async def test_external_mode() -> None:
    print("[external-openai] 启动")
    plugin = _make_plugin(_external_model())
    plugin._fetch_file_bytes = AsyncMock(return_value=(b"hello world", "test"))
    plugin._decode_text = MagicMock(return_value=("hello world content", False))

    fake_session = _mock_aiohttp_session({"choices": [{"message": {"content": "EXTERNAL_REPLY"}}]})
    with patch("plugin.aiohttp.ClientSession", return_value=fake_session):
        ok, msg, _ = await _run_handler(plugin)

    assert ok, f"external 应该成功，得到 {msg}"
    plugin.ctx.llm.generate.assert_not_awaited()
    args, kwargs = fake_session.post.call_args
    posted_url = args[0]
    assert posted_url == "https://example.com/v1/chat/completions", f"URL 不对：{posted_url}"
    body = kwargs.get("json")
    assert body["model"] == "gpt-4o-mini"
    assert body["temperature"] == 0.5
    assert body["max_tokens"] == 512
    # cache 友好结构：system + 文件 user + 要求 user
    roles = [m["role"] for m in body["messages"]]
    assert roles[0] == "system"
    assert roles.count("user") >= 2
    assert body["messages"][-1]["role"] == "user"
    assert "本次要求" in body["messages"][-1]["content"]
    assert "tools" in body and isinstance(body["tools"], list) and body["tools"]
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "submit_file_result"
    headers = kwargs.get("headers")
    assert headers["Authorization"] == "Bearer sk-test"
    sent_texts = [c.args[0] for c in plugin.ctx.send.text.await_args_list if c.args]
    assert "EXTERNAL_REPLY" in sent_texts
    _assert_reply_not_stored(plugin)
    print(f"[external] OK：POST {posted_url} model={body['model']} roles={roles} tools={len(body['tools'])}")


async def test_external_anthropic() -> None:
    print("[external-anthropic] 启动")
    plugin = _make_plugin(
        _external_model(
            provider="anthropic",
            api_base_url="https://api.anthropic.com",
            api_key="sk-ant-test",
            model_name="claude-sonnet-4-6",
        )
    )
    plugin._fetch_file_bytes = AsyncMock(return_value=(b"hello world", "test"))
    plugin._decode_text = MagicMock(return_value=("hello world content", False))

    fake_session = _mock_aiohttp_session({"content": [{"type": "text", "text": "ANTHROPIC_REPLY"}]})
    with patch("plugin.aiohttp.ClientSession", return_value=fake_session):
        ok, msg, _ = await _run_handler(plugin)

    assert ok, f"anthropic 应该成功，得到 {msg}"
    plugin.ctx.llm.generate.assert_not_awaited()
    args, kwargs = fake_session.post.call_args
    posted_url = args[0]
    assert posted_url == "https://api.anthropic.com/v1/messages", f"URL 不对：{posted_url}"
    headers = kwargs.get("headers")
    assert headers["x-api-key"] == "sk-ant-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in headers, f"anthropic 不应有 Authorization 头：{headers}"
    body = kwargs.get("json")
    assert body["model"] == "claude-sonnet-4-6"
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.5
    # Anthropic：system 顶层，messages 仅 user/assistant，tools 用 input_schema
    assert "system" in body and "文件内容处理助手" in body["system"]
    assert all(m["role"] in {"user", "assistant"} for m in body["messages"])
    assert body["messages"][-1]["role"] == "user"
    assert "本次要求" in body["messages"][-1]["content"]
    assert "tools" in body and body["tools"][0]["name"] == "submit_file_result"
    assert "input_schema" in body["tools"][0]
    sent_texts = [c.args[0] for c in plugin.ctx.send.text.await_args_list if c.args]
    assert "ANTHROPIC_REPLY" in sent_texts
    _assert_reply_not_stored(plugin)
    print(f"[external-anthropic] OK：POST {posted_url} model={body['model']}")


async def test_external_anthropic_base_with_v1() -> None:
    print("[external-anthropic-v1] 启动")
    plugin = _make_plugin(
        _external_model(
            provider="anthropic",
            api_base_url="https://api.anthropic.com/v1",
            api_key="sk-ant-test",
            model_name="claude-sonnet-4-6",
            temperature=0.3,
            max_tokens=256,
        )
    )
    plugin._fetch_file_bytes = AsyncMock(return_value=(b"hello world", "test"))
    plugin._decode_text = MagicMock(return_value=("hello world content", False))

    fake_session = _mock_aiohttp_session({"content": [{"type": "text", "text": "V1_NO_DUP_REPLY"}]})
    with patch("plugin.aiohttp.ClientSession", return_value=fake_session):
        ok, msg, _ = await _run_handler(plugin)

    assert ok, f"anthropic v1 base 应该成功，得到 {msg}"
    args, _kwargs = fake_session.post.call_args
    posted_url = args[0]
    assert posted_url == "https://api.anthropic.com/v1/messages", f"URL 不应重复 /v1：{posted_url}"
    assert "/v1/v1/" not in posted_url, f"URL 出现了重复 /v1：{posted_url}"
    plugin.ctx.llm.generate.assert_not_awaited()
    sent_texts = [c.args[0] for c in plugin.ctx.send.text.await_args_list if c.args]
    assert "V1_NO_DUP_REPLY" in sent_texts
    _assert_reply_not_stored(plugin)
    print(f"[external-anthropic-v1] OK：URL={posted_url} 无重复 /v1")


async def test_external_mode_bad_payload() -> None:
    print("[external-bad] 启动")
    plugin = _make_plugin(_external_model(api_base_url="https://example.com/v1", max_tokens=128, temperature=0.7))
    plugin._fetch_file_bytes = AsyncMock(return_value=(b"hello world", "test"))
    plugin._decode_text = MagicMock(return_value=("hello world content", False))

    fake_session = _mock_aiohttp_session({"error": "bad"})
    with patch("plugin.aiohttp.ClientSession", return_value=fake_session):
        ok, msg, _ = await _run_handler(plugin)
    assert not ok, "结构异常应该失败"
    assert "结构异常" in msg or "失败" in msg
    # 聊天侧不应出现完整 payload 泄露
    sent_texts = [str(c.args[0]) for c in plugin.ctx.send.text.await_args_list]
    assert not any("payload=" in t for t in sent_texts), f"不应把 payload 打到聊天：{sent_texts}"
    print(f"[external-bad] OK：错误信息={msg}")


async def test_external_missing_api_key() -> None:
    print("[external-no-key] 启动")
    plugin = _make_plugin(_external_model(api_key="   "))
    ok, msg, intercept = await _run_handler(plugin)
    assert not ok
    assert intercept
    assert "api_key" in msg.lower() or "API Key" in str(plugin.ctx.send.text.await_args_list)
    plugin.ctx.llm.generate.assert_not_awaited()
    # 未配置 key 时不应去取文件
    print(f"[external-no-key] OK：msg={msg}")


async def test_plugin_disabled() -> None:
    print("[disabled] 启动")
    plugin = _make_plugin(_host_model(), enabled=False)
    ok, msg, intercept = await _run_handler(plugin)
    assert not ok and intercept == 0
    assert "未启用" in msg
    plugin.ctx.send.text.assert_not_awaited()
    print("[disabled] OK")


async def test_empty_text_hint() -> None:
    print("[empty-text] 启动")
    plugin = _make_plugin(_host_model())
    plugin._fetch_file_bytes = AsyncMock(return_value=(b"\x00\x01\x02", "test"))
    plugin._decode_text = MagicMock(return_value=("", False))
    ok, msg, intercept = await _run_handler(plugin)
    assert not ok and intercept
    assert "无可读文本" in msg
    sent = " ".join(str(c.args[0]) for c in plugin.ctx.send.text.await_args_list)
    assert "纯文本" in sent
    print("[empty-text] OK")


# ---------- NapCat / 本地路径 / 解码 ----------


async def test_fetch_via_napcat_base64() -> None:
    print("[napcat-base64] 启动")
    plugin = _make_plugin(_host_model())
    raw = b"hello-from-base64"
    payload = {"status": "ok", "data": {"base64": base64.b64encode(raw).decode("ascii")}}
    fake_session = _mock_aiohttp_session(payload)
    with patch("plugin.aiohttp.ClientSession", return_value=fake_session):
        data = await plugin._fetch_via_napcat("file-id-1")
    assert data == raw
    print("[napcat-base64] OK")


async def test_fetch_via_napcat_local_allowed(tmp_path: Path | None = None) -> None:
    print("[napcat-local-allowed] 启动")
    work = Path(__file__).parent / "_test_tmp_allowed"
    work.mkdir(exist_ok=True)
    target = work / "sample.txt"
    target.write_bytes(b"local-file-content")

    plugin = _make_plugin(
        _host_model(),
        napcat=NapcatConfig(
            http_base_url="http://127.0.0.1:3000",
            access_token="",
            allowed_local_prefixes=str(work),
        ),
    )
    payload = {"status": "ok", "data": {"file": str(target)}}
    fake_session = _mock_aiohttp_session(payload)
    with patch("plugin.aiohttp.ClientSession", return_value=fake_session):
        data = await plugin._fetch_via_napcat("file-id-2")
    assert data == b"local-file-content"
    target.unlink(missing_ok=True)
    work.rmdir()
    print("[napcat-local-allowed] OK")


async def test_fetch_via_napcat_local_denied() -> None:
    print("[napcat-local-denied] 启动")
    # 用系统上几乎肯定存在但不在白名单的路径语义：白名单留空 → 一律拒绝
    plugin = _make_plugin(
        _host_model(),
        napcat=NapcatConfig(
            http_base_url="http://127.0.0.1:3000",
            access_token="",
            allowed_local_prefixes="",
        ),
    )
    payload = {"status": "ok", "data": {"file": str(Path(__file__).resolve())}}
    fake_session = _mock_aiohttp_session(payload)
    with patch("plugin.aiohttp.ClientSession", return_value=fake_session):
        try:
            await plugin._fetch_via_napcat("file-id-3")
            raise AssertionError("应拒绝本地路径")
        except ValueError as exc:
            assert "白名单" in str(exc) or "拒绝" in str(exc)
    print("[napcat-local-denied] OK")


async def test_decode_text_truncation() -> None:
    print("[decode-truncate] 启动")
    plugin = _make_plugin(
        _host_model(),
        read=ReadConfig(
            max_chars=10,
            max_download_bytes=5_000_000,
            timeout_seconds=30,
            default_requirement="概括",
        ),
    )
    text, truncated = plugin._decode_text("你好世界ABCDEFGHIJK".encode("utf-8"))
    assert truncated is True
    assert len(text) == 10
    print(f"[decode-truncate] OK：text={text!r}")


async def test_is_http_url() -> None:
    print("[is-http-url] 启动")
    assert FileReaderPlugin._is_http_url("https://example.com/a.txt")
    assert FileReaderPlugin._is_http_url("http://127.0.0.1:8080/f")
    assert not FileReaderPlugin._is_http_url("file-id-only")
    assert not FileReaderPlugin._is_http_url("ftp://x")
    assert not FileReaderPlugin._is_http_url("https://")
    print("[is-http-url] OK")


async def test_safe_summary() -> None:
    print("[safe-summary] 启动")
    long_val = {"a": "x" * 500}
    s = FileReaderPlugin._safe_summary(long_val, max_len=50)
    assert len(s) <= 50
    assert s.endswith("...")
    print("[safe-summary] OK")


async def test_missing_file_ref_usage() -> None:
    print("[usage] 启动")
    plugin = _make_plugin(_host_model())
    ok, msg, intercept = await plugin.handle_read_file(stream_id="s1", matched_groups={}, message={})
    assert not ok and intercept
    sent = str(plugin.ctx.send.text.await_args.args[0])
    assert "用法" in sent
    assert "引用" in sent or "纯文本" in sent
    print("[usage] OK")


async def test_split_body_with_context() -> None:
    print("[split-body] 启动")
    plugin = _make_plugin(_host_model())
    # 有引用上下文时，整段 body 都是要求
    file_ref, req = plugin._split_body("概括一下重点", has_context=True)
    assert file_ref == ""
    assert req == "概括一下重点"
    # URL 优先
    file_ref, req = plugin._split_body("https://a.com/x.txt 提取待办", has_context=True)
    assert file_ref == "https://a.com/x.txt"
    assert req == "提取待办"
    # 无上下文时第一段当 file_id
    file_ref, req = plugin._split_body("ABC123 总结", has_context=False)
    assert file_ref == "ABC123"
    assert req == "总结"
    print("[split-body] OK")


async def test_extract_reply_target_id() -> None:
    print("[reply-id] 启动")
    plugin = _make_plugin(_host_model())
    msg = {
        "raw_message": [
            {"type": "reply", "data": {"target_message_id": "998877"}},
            {"type": "text", "data": "/读文件 概括"},
        ]
    }
    assert plugin._extract_reply_target_id(msg) == "998877"
    assert plugin._extract_reply_target_id({"reply_to": "112233"}) == "112233"
    print("[reply-id] OK")


async def test_extract_file_ref_from_onebot_message() -> None:
    print("[onebot-file] 启动")
    plugin = _make_plugin(_host_model())
    detail = {
        "message": [
            {"type": "file", "data": {"file_id": "FID-001", "file": "notes.txt", "url": "https://cdn.example/a.txt"}},
        ]
    }
    # 优先 file_id
    assert plugin._extract_file_ref_from_onebot_message(detail) == "FID-001"
    detail2 = {
        "message": [
            {"type": "file", "data": {"file": "notes.txt", "url": "https://cdn.example/a.txt"}},
        ]
    }
    assert plugin._extract_file_ref_from_onebot_message(detail2) == "https://cdn.example/a.txt"
    print("[onebot-file] OK")


async def test_reply_message_resolves_file() -> None:
    print("[reply-resolve] 启动")
    plugin = _make_plugin(_host_model())
    plugin._fetch_file_bytes = AsyncMock(return_value=(b"hello world", "test"))
    plugin._decode_text = MagicMock(return_value=("hello world content", False))
    plugin._get_msg_detail = AsyncMock(
        return_value={
            "message": [
                {"type": "file", "data": {"file_id": "FROM-REPLY", "file": "a.txt"}},
            ]
        }
    )

    message = {
        "raw_message": [
            {"type": "reply", "data": {"target_message_id": "555"}},
            {"type": "text", "data": "/读文件 总结"},
        ]
    }
    ok, msg, _ = await _run_handler(plugin, body="总结", message=message)
    assert ok, f"引用解析应成功，得到 {msg}"
    plugin._get_msg_detail.assert_awaited_once_with("555")
    plugin._fetch_file_bytes.assert_awaited_once_with("FROM-REPLY")
    print("[reply-resolve] OK")


async def test_prompt_cache_friendly_order() -> None:
    print("[prompt-order] 启动")
    plugin = _make_plugin(_host_model())
    messages = plugin._build_prompt_messages("概括一下", "hello file body", False)
    assert messages[0]["role"] == "system"
    assert "文件内容处理助手" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "hello file body" in messages[1]["content"]
    assert "【文件正文】" in messages[1]["content"]
    assert messages[2]["role"] == "user"
    assert "【本次要求】" in messages[2]["content"]
    tools = plugin._get_fixed_tools()
    assert len(tools) >= 2
    assert tools[0]["function"]["name"] == "submit_file_result"
    host_tools = plugin._host_tool_definitions(tools)
    assert host_tools[0]["name"] == "submit_file_result"
    assert "parameters" in host_tools[0]
    # 常见短要求应映射到稳定模板
    resolved = plugin._resolve_requirement_text("概括一下")
    assert "概括" in resolved or "条目" in resolved
    # 长句不误伤
    long_req = "请只提取第 3 段里所有日期并按时间排序，不要总结"
    assert plugin._resolve_requirement_text(long_req) == long_req
    print("[prompt-order] OK")


async def test_user_access_policy() -> None:
    print("[user-access] 启动")
    from plugin import AccessConfig

    plugin = _make_plugin(_host_model())
    # 默认 all：全放行
    ok, reason = plugin._check_user_access("qq", "123")
    assert ok and reason == ""

    plugin.config.access = AccessConfig(access_mode="blacklist", user_blacklist="123,qq:456")
    ok, reason = plugin._check_user_access("qq", "123")
    assert not ok and "黑名单" in reason
    ok, reason = plugin._check_user_access("qq", "456")
    assert not ok and "黑名单" in reason
    ok, reason = plugin._check_user_access("qq", "789")
    assert ok

    plugin.config.access = AccessConfig(access_mode="whitelist", user_whitelist="qq:10001,20002")
    ok, reason = plugin._check_user_access("qq", "10001")
    assert ok
    ok, reason = plugin._check_user_access("qq", "30003")
    assert not ok and "白名单" in reason
    ok, reason = plugin._check_user_access("qq", "")
    assert not ok and "无法识别" in reason

    # whitelist 模式但名单为空：无人可用
    plugin.config.access = AccessConfig(access_mode="whitelist", user_whitelist="")
    ok, reason = plugin._check_user_access("qq", "1")
    assert not ok and "白名单为空" in reason
    print("[user-access] OK")


async def test_user_access_blocks_handler() -> None:
    print("[user-block-handler] 启动")
    from plugin import AccessConfig

    plugin = _make_plugin(
        _host_model(),
        access=AccessConfig(
            access_mode="blacklist",
            user_blacklist="10001",
            deny_message="禁止使用读文件",
            silent_deny=False,
        ),
    )
    plugin._fetch_file_bytes = AsyncMock(return_value=(b"x", "t"))
    ok, msg, intercept = await _run_handler(plugin, body="https://a.com/a.txt 概括", user_id="10001")
    assert not ok and intercept >= 1
    assert "无权限" in msg or "黑名单" in msg
    sent = " ".join(str(c.args[0]) for c in plugin.ctx.send.text.await_args_list if c.args)
    assert "禁止使用读文件" in sent
    plugin._fetch_file_bytes.assert_not_awaited()
    print(f"[user-block-handler] OK：{msg}")


async def test_cache_stats_formatting() -> None:
    print("[cache-stats] 启动")
    plugin = _make_plugin(_host_model())
    stats = plugin._extract_cache_stats(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 120,
            "prompt_cache_hit_tokens": 800,
            "prompt_cache_miss_tokens": 200,
        }
    )
    assert stats["prompt_cache_hit_rate"] == 80.0
    text = plugin._format_cache_stats(stats)
    assert "cache_hit_rate=80.0%" in text
    assert "hit=800" in text
    assert "miss=200" in text
    assert "prompt=1000" in text

    # usage 嵌套 + cached_tokens 细节
    stats2 = plugin._extract_cache_stats(
        {
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 100},
            }
        }
    )
    assert stats2["prompt_cache_hit_tokens"] == 100
    assert stats2["prompt_cache_miss_tokens"] == 400
    assert abs(float(stats2["prompt_cache_hit_rate"] or 0) - 20.0) < 1e-6

    # 无字段
    stats3 = plugin._extract_cache_stats({"response": "x"})
    assert plugin._format_cache_stats(stats3) == "cache=unavailable"
    print(f"[cache-stats] OK：{text}")


async def test_extract_tool_call_response() -> None:
    print("[tool-call-extract] 启动")
    plugin = _make_plugin(_host_model())
    text = plugin._extract_response_text(
        {
            "success": True,
            "response": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "submit_file_result",
                        "arguments": '{"result":"来自工具的结果","task_type":"summary"}',
                    }
                }
            ],
        }
    )
    assert text == "来自工具的结果"
    text2 = plugin._extract_response_text(
        {
            "success": True,
            "response": "",
            "tool_calls": [
                {"name": "report_file_limitation", "arguments": {"reason": "正文截断", "partial_result": "只能看到开头"}}
            ],
        }
    )
    assert "只能看到开头" in text2 and "正文截断" in text2
    print("[tool-call-extract] OK")


async def test_host_mode_receives_messages_list() -> None:
    print("[host-messages] 启动")
    plugin = _make_plugin(_host_model())
    plugin._fetch_file_bytes = AsyncMock(return_value=(b"hello world", "test"))
    plugin._decode_text = MagicMock(return_value=("hello world content", False))
    ok, msg, _ = await _run_handler(plugin, body="abc 总结")
    assert ok, msg
    args = plugin.ctx.llm.generate_with_tools.await_args
    prompt = args.args[0] if args.args else args.kwargs.get("prompt")
    assert isinstance(prompt, list), f"host 应收到 messages list，得到 {type(prompt)}"
    assert prompt[0]["role"] == "system"
    assert "hello world content" in prompt[1]["content"]
    assert "本次要求" in prompt[2]["content"]
    tools = args.kwargs.get("tools") or []
    assert tools and tools[0]["name"] == "submit_file_result"
    print("[host-messages] OK")


async def test_command_pattern_with_reply_prefix() -> None:
    """Host 用 re.search 匹配 processed_plain_text；引用前缀不能挡住命令。"""
    print("[pattern-prefix] 启动")
    import re
    from plugin import FileReaderPlugin

    # 从装饰器元数据或直接用当前源码约定的 pattern 复测
    pattern = re.compile(r"/(?:读文件|readfile)(?:[ \t]+(?P<body>.+?))?[ \t]*$")
    cases = {
        "/读文件": {"body": None},
        "/读文件 概括一下": {"body": "概括一下"},
        "[回复了谢小嘉的消息: [文件] a.py] /读文件": {"body": None},
        "[回复了谢小嘉的消息: [文件] a.py] /读文件 概括一下": {"body": "概括一下"},
        "[文件] x.py，大小: 1 /读文件 提取待办": {"body": "提取待办"},
        "普通聊天": None,
    }
    for text, expected in cases.items():
        m = pattern.search(text)
        got = m.groupdict() if m else None
        assert got == expected, f"text={text!r} got={got} expected={expected}"
    # 旧版 ^ 锚定会失败的关键用例
    old = re.compile(r"^/(?:读文件|readfile)(?:\s+(?P<body>.+))?$")
    assert old.search("[回复了x] /读文件 概括一下") is None
    assert pattern.search("[回复了x] /读文件 概括一下") is not None
    print("[pattern-prefix] OK")


async def test_reply_without_file_errors() -> None:
    print("[reply-no-file] 启动")
    plugin = _make_plugin(_host_model())
    plugin._get_msg_detail = AsyncMock(return_value={"message": [{"type": "text", "data": "hi"}]})
    message = {"raw_message": [{"type": "reply", "data": {"target_message_id": "1"}}]}
    ok, msg, intercept = await _run_handler(plugin, body="", message=message)
    assert not ok and intercept
    assert "文件" in msg or "引用" in msg
    print(f"[reply-no-file] OK：msg={msg}")


async def main() -> None:
    await test_host_mode()
    await test_host_mode_empty_task()
    await test_host_mode_receives_messages_list()
    await test_prompt_cache_friendly_order()
    await test_user_access_policy()
    await test_user_access_blocks_handler()
    await test_cache_stats_formatting()
    await test_extract_tool_call_response()
    await test_external_mode()
    await test_external_mode_bad_payload()
    await test_external_anthropic()
    await test_external_anthropic_base_with_v1()
    await test_external_missing_api_key()
    await test_plugin_disabled()
    await test_empty_text_hint()
    await test_fetch_via_napcat_base64()
    await test_fetch_via_napcat_local_allowed()
    await test_fetch_via_napcat_local_denied()
    await test_decode_text_truncation()
    await test_is_http_url()
    await test_safe_summary()
    await test_missing_file_ref_usage()
    await test_split_body_with_context()
    await test_extract_reply_target_id()
    await test_extract_file_ref_from_onebot_message()
    await test_reply_message_resolves_file()
    await test_command_pattern_with_reply_prefix()
    await test_reply_without_file_errors()
    print("\n全部测试通过 [OK]")


if __name__ == "__main__":
    asyncio.run(main())
