"""文件阅读插件

用户通过指令指定一个文件（NapCat file_id 或 http(s) URL），或**引用一条文件消息**，
插件下载该文件、按纯文本读取内容，连同用户要求一起交给配置指定的 LLM 处理，并回复。

指令格式：
- ``/读文件 <文件标识> [要求]``（别名 ``/readfile``）
- ``/读文件 [要求]`` —— 需引用/回复一条文件消息，或本条消息带文件

文件标识：NapCat 的 file_id，或 http(s):// 直链。
要求：可选，省略时默认「概括文件主要内容」。

取文件方式：
- http(s) 链接：插件直接下载
- 引用消息：从 reply 的 target_message_id 调 NapCat ``get_msg``，再取 file_id/url
- 其余按 NapCat file_id：OneBot HTTP ``get_file``（需开启 HTTP，**不走 WebSocket**）

限制：仅支持纯文本类文件。PDF/Office/图片等不会被解析。

依赖：``aiohttp``、``charset-normalizer``
"""

from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import base64 as base64_module

from charset_normalizer import from_bytes

import aiohttp

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

# 本地路径读取白名单：仅允许落在这些目录下（NapCat 缓存/临时目录常见位置）。
# 空列表表示拒绝一切本地路径，强制走 base64/url，避免任意文件读取。
_DEFAULT_LOCAL_PATH_PREFIXES: tuple[str, ...] = (
    "/tmp",
    "/var/tmp",
    "C:\\Windows\\Temp",
    "C:\\Users",
)

# 用于从引用消息拉取原消息（短名；Host 可解析到 napcat-adapter 公开 API）
_GET_MSG_API_CANDIDATES: tuple[str, ...] = (
    "adapter.napcat.message.get_msg",
    "maibot-team.napcat-adapter.adapter.napcat.message.get_msg",
)

# 固定 system 前缀：跨请求尽量不变，便于厂商 prompt cache 命中。
_FIXED_SYSTEM_INSTRUCTION = (
    "你是文件内容处理助手。只根据用户提供的文件正文完成任务，不要编造正文中不存在的信息。"
    "若正文被截断，请明确说明依据不完整。直接给出结果，不要复述全文。"
    "你可以使用提供的 tools 来组织输出；若无需工具参数，也可直接用自然语言回复最终结果。"
)

# 固定 tools：同一版本内定义完全不变，作为稳定请求前缀的一部分（类似 Claude Code）。
# 注意：这是“声明给模型的固定工具 schema”，不是去执行真实外部函数。
_FIXED_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "submit_file_result",
            "description": "提交对文件正文的最终处理结果。优先调用本工具给出结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "result": {
                        "type": "string",
                        "description": "面向用户的最终结果文本",
                    },
                    "task_type": {
                        "type": "string",
                        "description": "任务类型标签",
                        "enum": ["summary", "todos", "code_structure", "translate", "custom"],
                    },
                },
                "required": ["result"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_file_limitation",
            "description": "当文件内容不足、被截断或无法满足要求时，报告限制说明。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "无法完整完成任务的原因",
                    },
                    "partial_result": {
                        "type": "string",
                        "description": "在限制条件下仍可给出的部分结果，可为空",
                    },
                },
                "required": ["reason"],
            },
        },
    },
)

# 常用短要求 → 稳定模板文案（提高「同一类任务」时尾部前缀的一致性）
_REQUIREMENT_TEMPLATES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("概括", "总结", "summary", "summarize", "tl;dr", "tldr"),
        "请概括文件的主要内容，用简洁条目列出核心信息。",
    ),
    (
        ("待办", "todo", "todos", "任务列表"),
        "请从文件中提取待办/任务项，按条目列出；没有则说明未找到。",
    ),
    (
        ("函数", "方法", "api", "代码结构", "讲代码", "解释代码"),
        "请说明代码结构：模块职责、主要函数/类及其用途；不确定处请标明。",
    ),
    (
        ("翻译", "译成", "translate"),
        "请将文件内容翻译成中文，保留专有名词，不要添加原文没有的信息。",
    ),
)


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.2.8", description="配置版本")


class NapcatConfig(PluginConfigBase):
    """NapCat OneBot HTTP 服务配置"""

    __ui_label__ = "NapCat 连接"
    __ui_icon__ = "plug"
    __ui_order__ = 1

    http_base_url: str = Field(
        default="http://127.0.0.1:3000",
        description="NapCat OneBot HTTP 服务地址，如 http://127.0.0.1:3000（以本机实际端口为准）",
    )
    access_token: str = Field(default="", description="NapCat 配置的 access token，未设置则留空")
    allowed_local_prefixes: str = Field(
        default=",".join(_DEFAULT_LOCAL_PATH_PREFIXES),
        description="NapCat 返回本地路径时允许读取的目录前缀，逗号分隔；留空则禁止本地路径读取",
    )


class AccessConfig(PluginConfigBase):
    """用户访问控制（谁能使用 /读文件）"""

    __ui_label__ = "用户权限"
    __ui_icon__ = "users"
    __ui_order__ = 1.5

    # WebUI 会把 Literal 渲染成下拉：all / whitelist / blacklist
    access_mode: Literal["all", "whitelist", "blacklist"] = Field(
        default="all",
        description=(
            "访问模式（下拉选择）："
            "all=所有人可用；"
            "whitelist=仅白名单可用；"
            "blacklist=除黑名单外都可用"
        ),
        json_schema_extra={
            "label": "访问模式",
            "hint": "先选模式，再按需填写下方名单（all 可不填名单）",
            "order": 0,
            "x-widget": "select",
            "i18n": {
                "zh-CN": {
                    "label": "访问模式",
                    "hint": "先选模式：所有人 / 仅白名单 / 黑名单拦截",
                    "description": "all=所有人可用；whitelist=仅白名单；blacklist=黑名单拦截",
                }
            },
        },
    )
    # 逗号/换行分隔。支持纯数字 QQ 号，或 qq:123456
    user_whitelist: str = Field(
        default="",
        description="白名单用户列表（access_mode=whitelist 时生效）。例：123456,qq:789",
        json_schema_extra={
            "label": "白名单用户",
            "placeholder": "123456,qq:789012",
            "hint": "仅「whitelist」模式使用；支持 QQ 号或 qq:号",
            "order": 1,
            "rows": 3,
            "i18n": {
                "zh-CN": {
                    "label": "白名单用户",
                    "hint": "仅「仅白名单」模式生效；支持 QQ 号或 qq:号",
                }
            },
        },
    )
    user_blacklist: str = Field(
        default="",
        description="黑名单用户列表（access_mode=blacklist 时生效）。例：111,qq:222",
        json_schema_extra={
            "label": "黑名单用户",
            "placeholder": "111222,qq:333444",
            "hint": "仅「blacklist」模式使用；支持 QQ 号或 qq:号",
            "order": 2,
            "rows": 3,
            "i18n": {
                "zh-CN": {
                    "label": "黑名单用户",
                    "hint": "仅「黑名单拦截」模式生效；支持 QQ 号或 qq:号",
                }
            },
        },
    )
    deny_message: str = Field(
        default="你没有权限使用文件阅读功能。",
        description="用户被拒绝时的提示文案",
        json_schema_extra={
            "label": "拒绝提示",
            "placeholder": "你没有权限使用文件阅读功能。",
            "order": 3,
        },
    )
    silent_deny: bool = Field(
        default=False,
        description="拒绝时是否静默（true=不回复提示，仍拦截命令）",
        json_schema_extra={
            "label": "静默拒绝",
            "hint": "开启后被拒绝用户看不到提示",
            "order": 4,
            "x-widget": "switch",
        },
    )


class ReadConfig(PluginConfigBase):
    """文件读取与 LLM 处理配置"""

    __ui_label__ = "读取与处理"
    __ui_icon__ = "file-text"
    __ui_order__ = 2

    max_chars: int = Field(default=20000, ge=100, description="读入文件的最大字符数，超出部分截断，避免上下文过长")
    max_download_bytes: int = Field(default=5_000_000, ge=1024, description="允许下载的最大文件字节数")
    timeout_seconds: int = Field(default=30, ge=1, description="下载与 NapCat / 外部 API 请求的超时时间（秒）")
    default_requirement: str = Field(default="概括这个文件的主要内容", description="用户未提供要求时使用的默认要求")
    fixed_system_instruction: str = Field(
        default="",
        description="可选：追加到固定 system 前缀后的自定义说明（留空则只用内置固定说明）。改动会打断该段缓存",
    )
    use_requirement_templates: bool = Field(
        default=True,
        description="是否把常见短要求（概括/待办/讲代码等）映射为稳定任务模板，利于缓存与输出一致",
    )
    use_fixed_tools: bool = Field(
        default=True,
        description="是否在 LLM 请求中附带固定 tools 定义（利于 prompt 前缀稳定；DeepSeek 等对 tools 进前缀更友好）",
    )
    report_cache_stats: bool = Field(
        default=True,
        description="是否在处理完成后输出 prompt cache 命中统计（默认只写日志；见 report_cache_stats_to_chat）",
    )
    report_cache_stats_to_chat: bool = Field(
        default=False,
        description="是否把 cache 命中率也发到聊天（默认 false，避免刷屏；true 时追加一条统计消息）",
    )


class ModelConfig(PluginConfigBase):
    """LLM 模型选择配置

    支持两种模式：host=用宿主任务模型；external=插件直调 OpenAI / Anthropic 接口。
    """

    __ui_label__ = "模型选择"
    __ui_icon__ = "cpu"
    __ui_order__ = 3

    mode: Literal["host", "external"] = Field(default="host", description="host=宿主任务模型；external=外部 API")
    task_name: str = Field(default="", description="host 模式：宿主模型任务名（如 replyer/planner/utils），留空使用默认")
    provider: Literal["openai", "anthropic"] = Field(
        default="openai",
        description="external 模式：API 协议（openai 兼容 / anthropic 原生）",
    )
    api_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="external 模式：API Base URL；anthropic 填到 https://api.anthropic.com 即可，自动拼 /v1/messages",
    )
    api_key: str = Field(default="", description="external 模式：API Key")
    model_name: str = Field(default="gpt-4o-mini", description="external 模式：模型名称")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="external 模式：采样温度")
    max_tokens: int = Field(default=2048, ge=1, description="external 模式：生成最大 token 数")


class FileReaderPluginConfig(PluginConfigBase):
    """文件阅读插件配置"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    napcat: NapcatConfig = Field(default_factory=NapcatConfig)
    access: AccessConfig = Field(default_factory=AccessConfig)
    read: ReadConfig = Field(default_factory=ReadConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)


class FileReaderPlugin(MaiBotPlugin):
    """文件阅读插件"""

    config_model = FileReaderPluginConfig

    async def on_load(self) -> None:
        """处理插件加载"""
        self.ctx.logger.info(
            "文件阅读插件已加载 mode=%s provider=%s",
            self.config.model.mode,
            self.config.model.provider,
        )

    async def on_unload(self) -> None:
        """处理插件卸载"""
        self.ctx.logger.info("文件阅读插件已卸载")

    async def on_config_update(self, scope: str, _config_data: dict[str, Any], version: str) -> None:
        """处理配置热重载事件"""
        self.ctx.logger.info(
            "文件阅读插件配置已更新 scope=%s version=%s mode=%s",
            scope,
            version,
            self.config.model.mode,
        )

    async def _reply_text(self, text: str, stream_id: str) -> None:
        """发送插件回复，且不写入会话历史/Maisaka 上下文。

        Host 的 send.text 默认 storage_message=True，会把插件输出当成普通聊天入库，
        进而触发 Maisaka 再接一句闲聊。命令场景下显式关闭入库与 history 同步。
        """

        await self.ctx.send.text(
            text,
            stream_id,
            storage_message=False,
            sync_to_maisaka_history=False,
        )

    @Command(
        "read_file",
        description="读取指定/引用的文件并交给 LLM 处理（仅纯文本）",
        # 注意：Host 用 re.search 匹配 processed_plain_text。
        # 引用消息时正文前会带 [回复了...] / [文件]... 等前缀，不能用 ^ 锚定行首。
        pattern=r"/(?:读文件|readfile)(?:[ \t]+(?P<body>.+?))?[ \t]*$",
    )
    async def handle_read_file(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """处理 /读文件 指令

        Args:
            stream_id: 当前聊天流 ID
            **kwargs: Command 透传参数，含 ``matched_groups``、``message``

        Returns:
            tuple[bool, str, int]: ``(是否成功, 结果说明, intercept_message_level)``。
            第三项为拦截等级：``0`` 不拦截后续主链，``>=1`` 拦截（避免 HeartFlow/Maisaka 再回复）。
        """

        # 非 0 即拦截后续主链；用 2 与常见命令拦截等级对齐，避免上下文过滤把命令消息又捞回去。
        intercept_level = 2

        if not self.config.plugin.enabled:
            return False, "插件未启用", 0

        matched_groups = kwargs.get("matched_groups")
        if not isinstance(matched_groups, dict):
            matched_groups = {}

        body = str(matched_groups.get("body") or "").strip()
        message = kwargs.get("message")
        if not isinstance(message, dict):
            message = {}

        # 用户访问控制：由 access.access_mode 决定（all / whitelist / blacklist）
        platform = str(kwargs.get("platform") or "").strip()
        user_id = str(kwargs.get("user_id") or "").strip()
        if not platform or not user_id:
            platform, user_id = self._extract_user_from_message(message, platform=platform, user_id=user_id)
        allowed_user, deny_reason = self._check_user_access(platform, user_id)
        if not allowed_user:
            self.ctx.logger.info(
                "用户无权限使用读文件 platform=%s user_id=%s reason=%s",
                platform,
                user_id,
                deny_reason,
            )
            if not self.config.access.silent_deny:
                await self._reply_text(self.config.access.deny_message, stream_id)
            return False, f"用户无权限: {deny_reason}", intercept_level

        has_context = self._message_has_file_context(message)
        file_ref, requirement = self._split_body(body, has_context=has_context)

        if not file_ref:
            try:
                file_ref = await self._resolve_file_ref_from_message(message)
            except Exception as exc:
                self.ctx.logger.warning("从引用/消息解析文件失败: %s", exc)
                await self._reply_text(f"从引用消息获取文件失败：{exc}", stream_id)
                return False, f"引用解析失败: {exc}", intercept_level

        if not file_ref:
            await self._reply_text(
                "用法：\n"
                "1) /读文件 <文件ID或链接> [要求]\n"
                "2) 先引用一条文件消息，再发：/读文件 [要求]\n"
                "说明：仅支持纯文本（.txt/.md/.json/.log/源码等），不解析 PDF/Office/图片。",
                stream_id,
            )
            return False, "缺少文件标识", intercept_level

        if not requirement:
            requirement = self.config.read.default_requirement

        if self.config.model.mode == "external" and not str(self.config.model.api_key or "").strip():
            await self._reply_text(
                "外部模型模式未配置 API Key，请在插件配置 [model] 中填写 api_key，或将 mode 改为 host。",
                stream_id,
            )
            return False, "external 模式缺少 api_key", intercept_level

        # 1. 取文件字节
        try:
            file_bytes, source_desc = await self._fetch_file_bytes(file_ref)
        except Exception as exc:
            self.ctx.logger.warning("获取文件失败 file_ref=%s: %s", file_ref, exc)
            await self._reply_text(f"获取文件失败：{exc}", stream_id)
            return False, f"获取文件失败: {exc}", intercept_level

        # 2. 按纯文本解码
        text_content, truncated = self._decode_text(file_bytes)
        if not text_content.strip():
            await self._reply_text(
                "文件内容为空或无法按文本读取。\n"
                "本插件仅支持纯文本类文件（.txt/.md/.json/.log/源码等），"
                "不支持 PDF、Word、Excel、图片等二进制格式。",
                stream_id,
            )
            return False, "文件无可读文本", intercept_level

        # 3. 交给 LLM 处理
        # 结构：固定 system + 固定 tools + 文件正文 + 本次要求（易变垫底）
        await self._reply_text("已读取文件，正在处理……", stream_id)
        prompt_messages = self._build_prompt_messages(requirement, text_content, truncated)
        fixed_tools = self._get_fixed_tools()
        if self.config.model.mode == "external":
            result = await self._generate_external(prompt_messages, tools=fixed_tools)
        else:
            task_name = self.config.model.task_name.strip() or None
            if fixed_tools:
                result = await self.ctx.llm.generate_with_tools(
                    prompt_messages,
                    tools=self._host_tool_definitions(fixed_tools),
                    model=task_name,
                )
            else:
                result = await self.ctx.llm.generate(prompt_messages, model=task_name)

        if not result.get("success"):
            reason = str(result.get("reasoning") or result.get("response") or "未知错误")
            self.ctx.logger.warning("LLM 处理失败 source=%s reason=%s", source_desc, reason)
            await self._reply_text(f"LLM 处理失败：{reason}", stream_id)
            return False, f"LLM 处理失败: {reason}", intercept_level

        response_text = self._extract_response_text(result)
        if not response_text:
            await self._reply_text("LLM 未返回有效内容", stream_id)
            return False, "LLM 返回空", intercept_level

        await self._reply_text(response_text, stream_id)

        cache_stats = self._extract_cache_stats(result)
        cache_summary = self._format_cache_stats(cache_stats)
        self.ctx.logger.info(
            "文件处理完成 source=%s model=%s truncated=%s chars=%s %s",
            source_desc,
            result.get("model") or result.get("model_name"),
            truncated,
            len(text_content),
            cache_summary,
        )
        if self.config.read.report_cache_stats and self.config.read.report_cache_stats_to_chat and cache_summary:
            await self._reply_text(f"[cache] {cache_summary}", stream_id)

        return True, "文件已处理并回复", intercept_level

    # ------------------------------------------------------------------
    # 用户黑白名单
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_id_list(raw: str) -> set[str]:
        """解析逗号/换行/分号分隔的 ID 列表，统一小写去空白。"""

        text = str(raw or "").replace("\r", "\n").replace(";", ",").replace("\n", ",")
        result: set[str] = set()
        for part in text.split(","):
            item = part.strip().lower()
            if item:
                result.add(item)
        return result

    @staticmethod
    def _extract_user_from_message(
        message: dict[str, Any],
        *,
        platform: str = "",
        user_id: str = "",
    ) -> tuple[str, str]:
        """从 Host 消息字典兜底提取 platform / user_id。"""

        resolved_platform = str(platform or "").strip()
        resolved_user_id = str(user_id or "").strip()
        if not isinstance(message, dict):
            return resolved_platform, resolved_user_id

        if not resolved_platform:
            resolved_platform = str(message.get("platform") or "").strip()

        message_info = message.get("message_info")
        if isinstance(message_info, dict):
            if not resolved_platform:
                resolved_platform = str(message_info.get("platform") or "").strip()
            user_info = message_info.get("user_info")
            if isinstance(user_info, dict) and not resolved_user_id:
                resolved_user_id = str(
                    user_info.get("user_id")
                    or user_info.get("id")
                    or user_info.get("uin")
                    or ""
                ).strip()

        if not resolved_user_id:
            resolved_user_id = str(
                message.get("user_id")
                or message.get("sender_id")
                or ""
            ).strip()
            sender = message.get("sender")
            if not resolved_user_id and isinstance(sender, dict):
                resolved_user_id = str(sender.get("user_id") or sender.get("id") or "").strip()

        return resolved_platform, resolved_user_id

    def _check_user_access(self, platform: str, user_id: str) -> tuple[bool, str]:
        """检查用户是否允许使用本插件。

        由 ``access.access_mode`` 决定策略：
        - ``all``：所有人可用
        - ``whitelist``：仅白名单
        - ``blacklist``：黑名单拦截，其余可用
        """

        mode = str(getattr(self.config.access, "access_mode", "all") or "all").strip().lower()
        if mode not in {"all", "whitelist", "blacklist"}:
            mode = "all"

        if mode == "all":
            return True, ""

        whitelist = self._parse_id_list(self.config.access.user_whitelist)
        blacklist = self._parse_id_list(self.config.access.user_blacklist)
        normalized_platform = str(platform or "").strip().lower()
        normalized_user_id = str(user_id or "").strip().lower()

        if not normalized_user_id:
            # 启用了名单模式但拿不到用户 ID：保守拒绝，避免被绕过
            return False, "无法识别用户 ID"

        candidates = {normalized_user_id}
        if normalized_platform:
            candidates.add(f"{normalized_platform}:{normalized_user_id}")

        if mode == "blacklist":
            if not blacklist:
                # 选了黑名单模式但名单为空：等同全放行
                return True, ""
            if candidates & blacklist:
                return False, "用户在黑名单中"
            return True, ""

        # whitelist 模式
        if not whitelist:
            return False, "白名单为空，无人可用"
        if candidates & whitelist:
            return True, ""
        return False, "用户不在白名单中"

    # ------------------------------------------------------------------
    # 命令参数 / 引用消息解析
    # ------------------------------------------------------------------

    def _split_body(self, body: str, *, has_context: bool) -> tuple[str, str]:
        """把指令正文拆成 ``(file_ref, requirement)``。

        规则：
        - 正文以 http(s) URL 开头 → 第一段是文件，其余是要求
        - 存在引用/本条文件上下文 → 整段正文都当要求（文件从上下文取）
        - 否则 → 第一段当 file_id，其余当要求（兼容旧用法）
        """

        text = str(body or "").strip()
        if not text:
            return "", ""

        parts = text.split(None, 1)
        first = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""

        if self._is_http_url(first):
            return first, rest

        if has_context:
            return "", text

        return first, rest

    def _message_has_file_context(self, message: dict[str, Any]) -> bool:
        """判断消息是否带引用，或本条已能直接抽出文件标识。"""

        if self._extract_reply_target_id(message):
            return True
        if self._extract_file_ref_from_host_message(message):
            return True
        return False

    def _iter_message_segments(self, message: dict[str, Any]) -> list[Any]:
        """收集 Host 消息字典中的消息段列表。"""

        segments: list[Any] = []
        raw_message = message.get("raw_message")
        if isinstance(raw_message, list):
            segments.extend(raw_message)

        # 部分序列化路径可能挂在 message 字段
        nested = message.get("message")
        if isinstance(nested, list):
            segments.extend(nested)
        return segments

    def _extract_reply_target_id(self, message: dict[str, Any]) -> str:
        """从消息中提取被引用消息 ID。"""

        reply_to = str(message.get("reply_to") or "").strip()
        if reply_to:
            return reply_to

        for segment in self._iter_message_segments(message):
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type") or "").strip().lower()
            data = segment.get("data")
            if segment_type == "reply":
                if isinstance(data, dict):
                    target = str(
                        data.get("target_message_id")
                        or data.get("id")
                        or data.get("message_id")
                        or ""
                    ).strip()
                    if target:
                        return target
                else:
                    target = str(data or "").strip()
                    if target:
                        return target
            # dict 组件包装
            if segment_type == "dict" and isinstance(data, dict):
                inner_type = str(data.get("type") or "").strip().lower()
                inner_data = data.get("data", data)
                if inner_type == "reply" and isinstance(inner_data, dict):
                    target = str(
                        inner_data.get("target_message_id")
                        or inner_data.get("id")
                        or inner_data.get("message_id")
                        or ""
                    ).strip()
                    if target:
                        return target
        return ""

    def _pick_file_ref_from_mapping(self, data: dict[str, Any]) -> str:
        """从文件段 data 中挑选可用的 file_id 或 url（优先 file_id，其次 url）。"""

        file_id = str(data.get("file_id") or data.get("id") or "").strip()
        if file_id and not self._looks_like_plain_filename(file_id):
            return file_id

        for key in ("url", "file_url"):
            url = str(data.get(key) or "").strip()
            if self._is_http_url(url):
                return url

        file_field = str(data.get("file") or "").strip()
        if self._is_http_url(file_field):
            return file_field

        # 某些实现把 id 放在 file 字段且不是文件名
        if file_field and not self._looks_like_plain_filename(file_field):
            return file_field

        if file_id:
            return file_id
        return ""

    @staticmethod
    def _looks_like_plain_filename(value: str) -> bool:
        """粗略判断字符串是否像普通文件名（避免把 会议纪要.txt 当 file_id）。"""

        text = str(value or "").strip()
        if not text or "/" in text or "\\" in text:
            return False
        lower = text.lower()
        return any(
            lower.endswith(ext)
            for ext in (
                ".txt",
                ".md",
                ".json",
                ".log",
                ".csv",
                ".py",
                ".js",
                ".ts",
                ".pdf",
                ".doc",
                ".docx",
                ".xls",
                ".xlsx",
                ".zip",
                ".rar",
                ".7z",
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".webp",
            )
        )

    def _extract_file_ref_from_segments(self, segments: list[Any]) -> str:
        """从消息段列表中提取第一个可用文件标识。"""

        for segment in segments:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type") or "").strip().lower()
            data = segment.get("data")

            if segment_type == "file":
                if isinstance(data, dict):
                    ref = self._pick_file_ref_from_mapping(data)
                    if ref:
                        return ref
                continue

            if segment_type == "dict" and isinstance(data, dict):
                inner_type = str(data.get("type") or "").strip().lower()
                inner_data = data.get("data", data)
                if inner_type == "file" and isinstance(inner_data, dict):
                    ref = self._pick_file_ref_from_mapping(inner_data)
                    if ref:
                        return ref
                # 有的实现直接把 file 字段摊在 dict data 上
                if "file_id" in data or "file_url" in data:
                    ref = self._pick_file_ref_from_mapping(data)
                    if ref:
                        return ref
        return ""

    def _extract_file_ref_from_host_message(self, message: dict[str, Any]) -> str:
        """从 Host 下发的当前消息字典中提取文件标识。"""

        return self._extract_file_ref_from_segments(self._iter_message_segments(message))

    def _extract_file_ref_from_onebot_message(self, detail: Any) -> str:
        """从 NapCat ``get_msg`` 返回的消息详情中提取文件标识。"""

        if not isinstance(detail, dict):
            return ""

        # 常见字段：message / raw_message / message_list
        candidates: list[Any] = []
        for key in ("message", "raw_message", "message_list"):
            value = detail.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, dict):
                # 偶发单段
                candidates.append(value)

        ref = self._extract_file_ref_from_segments(candidates)
        if ref:
            return ref

        # 有的实现把文件元数据挂在顶层
        if any(k in detail for k in ("file_id", "url", "file")):
            return self._pick_file_ref_from_mapping(detail)
        return ""

    async def _get_msg_detail(self, message_id: str) -> dict[str, Any] | None:
        """通过 napcat-adapter 公开 API 获取消息详情。"""

        last_error: Exception | None = None
        for api_name in _GET_MSG_API_CANDIDATES:
            try:
                result = await self.ctx.api.call(api_name, message_id=message_id)
                if isinstance(result, dict):
                    return result
                if result is None:
                    return None
                self.ctx.logger.warning("get_msg 返回非字典: type=%s api=%s", type(result).__name__, api_name)
                return None
            except Exception as exc:
                last_error = exc
                self.ctx.logger.debug("调用 %s 失败: %s", api_name, exc)
                continue

        if last_error is not None:
            raise RuntimeError(
                f"无法通过 napcat-adapter 获取消息 {message_id}（请确认适配器已加载且 API 可用）: {last_error}"
            )
        return None

    async def _resolve_file_ref_from_message(self, message: dict[str, Any]) -> str:
        """从当前消息或引用消息解析文件标识。"""

        # 1) 本条消息自带文件段（若适配器保留了结构化 file）
        direct = self._extract_file_ref_from_host_message(message)
        if direct:
            return direct

        # 2) 引用消息 → get_msg → 抽 file_id/url
        reply_id = self._extract_reply_target_id(message)
        if not reply_id:
            return ""

        detail = await self._get_msg_detail(reply_id)
        if not detail:
            raise ValueError(f"get_msg 未返回消息内容（message_id={reply_id}）")

        ref = self._extract_file_ref_from_onebot_message(detail)
        if not ref:
            raise ValueError("被引用的消息里没有可识别的文件（需要原消息含 file 段/file_id）")
        return ref

    # ------------------------------------------------------------------
    # 取文件
    # ------------------------------------------------------------------

    async def _fetch_file_bytes(self, file_ref: str) -> tuple[bytes, str]:
        """根据文件标识获取文件字节"""

        if self._is_http_url(file_ref):
            return await self._download_url(file_ref), f"url:{file_ref}"

        return await self._fetch_via_napcat(file_ref), f"napcat:{file_ref}"

    @staticmethod
    def _is_http_url(value: str) -> bool:
        """判断字符串是否为 http(s) URL"""

        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    async def _download_url(self, url: str) -> bytes:
        """下载 http(s) 直链文件"""

        read_config = self.config.read
        timeout = aiohttp.ClientTimeout(total=read_config.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                return await self._read_capped(response, read_config.max_download_bytes)

    async def _fetch_via_napcat(self, file_id: str) -> bytes:
        """通过 NapCat OneBot ``get_file`` action 获取文件

        对返回数据做多字段兜底：优先 ``base64``，其次 ``url`` 直链下载，最后本地 ``file`` 路径读取
        （本地路径受 ``allowed_local_prefixes`` 白名单约束）。
        """

        napcat_config = self.config.napcat
        base_url = napcat_config.http_base_url.rstrip("/")
        timeout = aiohttp.ClientTimeout(total=self.config.read.timeout_seconds)
        headers: dict[str, str] = {}
        if napcat_config.access_token:
            headers["Authorization"] = f"Bearer {napcat_config.access_token}"

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.post(f"{base_url}/get_file", json={"file_id": file_id, "file": file_id}) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)

        if not isinstance(payload, dict) or payload.get("status") == "failed":
            raise ValueError(f"NapCat get_file 返回异常：{self._safe_summary(payload)}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"NapCat get_file 未返回 data：{self._safe_summary(payload)}")

        base64_content = str(data.get("base64") or "").strip()
        if base64_content:
            raw = base64_module.b64decode(base64_content)
            if len(raw) > self.config.read.max_download_bytes:
                raise ValueError(f"文件超过大小上限（{self.config.read.max_download_bytes} 字节）")
            return raw

        file_url = str(data.get("url") or "").strip()
        if self._is_http_url(file_url):
            return await self._download_url(file_url)

        local_path = str(data.get("file") or data.get("path") or "").strip()
        if local_path:
            return self._read_local_file(local_path)

        raise ValueError("NapCat get_file 返回数据缺少可用的 base64/url/file 字段")

    def _read_local_file(self, local_path: str) -> bytes:
        """在白名单约束下读取本地文件"""

        path = Path(local_path).expanduser()
        try:
            resolved = path.resolve(strict=False)
        except OSError as exc:
            raise ValueError(f"无法解析本地路径：{exc}") from exc

        if not self._is_local_path_allowed(resolved):
            raise ValueError(
                "本地路径不在允许的目录白名单内，已拒绝读取。"
                "请在 [napcat] allowed_local_prefixes 中配置，或让 NapCat 返回 base64/url。"
            )

        if not resolved.is_file():
            raise ValueError("本地路径不是有效文件")

        size = resolved.stat().st_size
        max_bytes = self.config.read.max_download_bytes
        if size > max_bytes:
            raise ValueError(f"文件超过大小上限（{max_bytes} 字节）")

        return resolved.read_bytes()

    def _is_local_path_allowed(self, resolved: Path) -> bool:
        """判断解析后的本地路径是否落在白名单前缀下"""

        raw = str(self.config.napcat.allowed_local_prefixes or "").strip()
        if not raw:
            return False

        resolved_str = str(resolved)
        resolved_cmp = resolved_str.replace("/", "\\").lower()
        for prefix in raw.split(","):
            prefix = prefix.strip()
            if not prefix:
                continue
            try:
                prefix_resolved = str(Path(prefix).expanduser().resolve(strict=False))
            except OSError:
                prefix_resolved = prefix
            prefix_cmp = prefix_resolved.replace("/", "\\").lower()
            if resolved_cmp == prefix_cmp or resolved_cmp.startswith(prefix_cmp.rstrip("\\") + "\\"):
                return True
            if resolved_str == prefix_resolved or resolved_str.startswith(prefix_resolved.rstrip("/") + "/"):
                return True
        return False

    @staticmethod
    async def _read_capped(response: aiohttp.ClientResponse, max_bytes: int) -> bytes:
        """按大小上限分块读取响应体"""

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"文件超过大小上限（{max_bytes} 字节）")
            chunks.append(chunk)
        return b"".join(chunks)

    def _decode_text(self, file_bytes: bytes) -> tuple[str, bool]:
        """将文件字节按纯文本解码，并按字符上限截断"""

        best_match = from_bytes(file_bytes).best()
        text = str(best_match) if best_match is not None else file_bytes.decode("utf-8", errors="replace")

        max_chars = self.config.read.max_chars
        if len(text) > max_chars:
            return text[:max_chars], True
        return text, False

    def _get_fixed_tools(self) -> list[dict[str, Any]]:
        """返回固定 tools 定义的深拷贝列表；关闭时返回空列表。"""

        if not self.config.read.use_fixed_tools:
            return []
        # 返回拷贝，避免调用方/序列化副作用改到模块常量
        return [dict(tool) for tool in _FIXED_TOOLS]

    @staticmethod
    def _host_tool_definitions(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把 OpenAI 风格 tools 转成 Host normalize_tool_options 更易吃的扁平定义。"""

        normalized: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function") if tool.get("type") == "function" else tool
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            parameters = function.get("parameters") or function.get("parameters_schema") or {
                "type": "object",
                "properties": {},
                "required": [],
            }
            normalized.append(
                {
                    "name": name,
                    "description": str(function.get("description") or "").strip(),
                    "parameters": parameters,
                    "parameters_schema": parameters,
                }
            )
        return normalized

    @staticmethod
    def _to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """OpenAI tools → Anthropic tools 字段。"""

        converted: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            parameters = function.get("parameters") or function.get("input_schema") or {
                "type": "object",
                "properties": {},
                "required": [],
            }
            converted.append(
                {
                    "name": name,
                    "description": str(function.get("description") or "").strip(),
                    "input_schema": parameters,
                }
            )
        return converted

    @staticmethod
    def _extract_cache_stats(result: dict[str, Any]) -> dict[str, int | float | None]:
        """从 LLM 结果中提取 prompt cache 统计。

        兼容字段：
        - prompt_tokens / completion_tokens / total_tokens
        - prompt_cache_hit_tokens / prompt_cache_miss_tokens
        - usage 嵌套字典（external 原始 payload 解包后也可能出现）
        - prompt_tokens_details.cached_tokens
        """

        def _as_nonneg_int(value: Any) -> int | None:
            try:
                if value is None or value == "":
                    return None
                number = int(value)
            except (TypeError, ValueError):
                return None
            return max(number, 0)

        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        details = result.get("prompt_tokens_details")
        if not isinstance(details, dict):
            details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else {}
        if not isinstance(details, dict):
            details = {}

        prompt_tokens = _as_nonneg_int(
            result.get("prompt_tokens")
            if result.get("prompt_tokens") is not None
            else usage.get("prompt_tokens")
        )
        completion_tokens = _as_nonneg_int(
            result.get("completion_tokens")
            if result.get("completion_tokens") is not None
            else usage.get("completion_tokens")
        )
        total_tokens = _as_nonneg_int(
            result.get("total_tokens") if result.get("total_tokens") is not None else usage.get("total_tokens")
        )
        hit = _as_nonneg_int(
            result.get("prompt_cache_hit_tokens")
            if result.get("prompt_cache_hit_tokens") is not None
            else usage.get("prompt_cache_hit_tokens")
        )
        if hit is None:
            hit = _as_nonneg_int(details.get("cached_tokens") or details.get("cache_tokens"))

        miss = _as_nonneg_int(
            result.get("prompt_cache_miss_tokens")
            if result.get("prompt_cache_miss_tokens") is not None
            else usage.get("prompt_cache_miss_tokens")
        )
        if miss is None and hit is not None and prompt_tokens is not None:
            miss = max(prompt_tokens - hit, 0)
        if miss is None and hit is None and prompt_tokens is not None:
            # 没有 cache 字段时，按“全部未命中”展示，避免假命中
            hit = 0
            miss = prompt_tokens

        hit_rate: float | None = None
        if hit is not None and miss is not None:
            denom = hit + miss
            if denom > 0:
                hit_rate = hit * 100.0 / denom
            elif prompt_tokens == 0:
                hit_rate = 0.0

        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "prompt_cache_hit_tokens": hit,
            "prompt_cache_miss_tokens": miss,
            "prompt_cache_hit_rate": hit_rate,
        }

    @staticmethod
    def _format_cache_stats(stats: dict[str, int | float | None]) -> str:
        """把 cache 统计格式化成一行可读文本。"""

        prompt_tokens = stats.get("prompt_tokens")
        hit = stats.get("prompt_cache_hit_tokens")
        miss = stats.get("prompt_cache_miss_tokens")
        rate = stats.get("prompt_cache_hit_rate")
        completion = stats.get("completion_tokens")

        if prompt_tokens is None and hit is None and miss is None:
            return "cache=unavailable"

        parts: list[str] = []
        if rate is not None and hit is not None and miss is not None:
            parts.append(f"cache_hit_rate={rate:.1f}%")
            parts.append(f"hit={hit}")
            parts.append(f"miss={miss}")
        elif hit is not None:
            parts.append(f"hit={hit}")
            if miss is not None:
                parts.append(f"miss={miss}")
        else:
            parts.append("cache=unknown")

        if prompt_tokens is not None:
            parts.append(f"prompt={prompt_tokens}")
        if completion is not None:
            parts.append(f"completion={completion}")
        return " ".join(parts)

    @staticmethod
    def _extract_response_text(result: dict[str, Any]) -> str:
        """从 generate / generate_with_tools 结果中提取最终文本。

        优先普通 response；若模型走了 tool_calls，则解析 submit_file_result /
        report_file_limitation 的参数作为回复。
        """

        direct = str(result.get("response") or "").strip()
        if direct:
            return direct

        tool_calls = result.get("tool_calls")
        if not isinstance(tool_calls, list):
            return ""

        import json

        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            # Host / OpenAI 多种形状兼容
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(
                call.get("name")
                or call.get("func_name")
                or function.get("name")
                or ""
            ).strip()
            raw_args = (
                call.get("arguments")
                or call.get("args")
                or call.get("input")
                or function.get("arguments")
                or {}
            )
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    parsed_args = {"result": raw_args}
            elif isinstance(raw_args, dict):
                parsed_args = raw_args
            else:
                parsed_args = {}

            if name == "submit_file_result":
                text = str(parsed_args.get("result") or "").strip()
                if text:
                    return text
            if name == "report_file_limitation":
                reason = str(parsed_args.get("reason") or "").strip()
                partial = str(parsed_args.get("partial_result") or "").strip()
                if reason and partial:
                    return f"{partial}\n\n（限制说明：{reason}）"
                if partial:
                    return partial
                if reason:
                    return f"无法完整完成：{reason}"
            # 未知工具：若有 result/text 字段也兜底
            for key in ("result", "text", "content", "message"):
                text = str(parsed_args.get(key) or "").strip()
                if text:
                    return text
        return ""

    async def _generate_external(
        self,
        prompt: str | list[dict[str, str]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """直接调用外部 API 生成（按 provider 分派到 openai / anthropic）"""

        model_config = self.config.model
        if not str(model_config.api_key or "").strip():
            return {
                "success": False,
                "response": "",
                "reasoning": "external 模式未配置 api_key",
                "model": model_config.model_name,
            }

        base_url = model_config.api_base_url.rstrip("/")
        openai_messages, anthropic_system, anthropic_messages = self._split_prompt_for_providers(prompt)
        active_tools = list(tools or [])

        if model_config.provider == "anthropic":
            url = f"{base_url}/messages" if base_url.endswith("/v1") else f"{base_url}/v1/messages"
            headers = {
                "Content-Type": "application/json",
                "x-api-key": model_config.api_key,
                "anthropic-version": "2023-06-01",
            }
            body: dict[str, Any] = {
                "model": model_config.model_name,
                "max_tokens": model_config.max_tokens,
                "temperature": model_config.temperature,
                "messages": anthropic_messages,
            }
            if anthropic_system:
                body["system"] = anthropic_system
            if active_tools:
                body["tools"] = self._to_anthropic_tools(active_tools)
        else:
            url = f"{base_url}/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {model_config.api_key}",
            }
            body = {
                "model": model_config.model_name,
                "messages": openai_messages,
                "temperature": model_config.temperature,
                "max_tokens": model_config.max_tokens,
            }
            if active_tools:
                body["tools"] = active_tools

        timeout = aiohttp.ClientTimeout(total=self.config.read.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=body, headers=headers) as response:
                    response.raise_for_status()
                    payload = await response.json(content_type=None)
        except Exception as exc:
            self.ctx.logger.warning("外部接口请求失败 provider=%s: %s", model_config.provider, exc)
            return {
                "success": False,
                "response": "",
                "reasoning": f"外部接口请求失败：{exc}",
                "model": model_config.model_name,
            }

        usage = payload.get("usage") if isinstance(payload, dict) else None
        usage_fields: dict[str, Any] = {}
        if isinstance(usage, dict):
            usage_fields = {
                "usage": usage,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "prompt_cache_hit_tokens": usage.get("prompt_cache_hit_tokens"),
                "prompt_cache_miss_tokens": usage.get("prompt_cache_miss_tokens"),
                "prompt_tokens_details": usage.get("prompt_tokens_details"),
            }

        try:
            if model_config.provider == "anthropic":
                content = ""
                tool_calls: list[dict[str, Any]] = []
                for block in payload.get("content", []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and not content:
                        content = str(block.get("text") or "").strip()
                    if block.get("type") == "tool_use":
                        tool_calls.append(
                            {
                                "name": str(block.get("name") or "").strip(),
                                "arguments": block.get("input") or {},
                            }
                        )
                # Anthropic 可能把 cache 统计放在 usage.cache_read_input_tokens
                if isinstance(usage, dict):
                    if usage_fields.get("prompt_cache_hit_tokens") is None:
                        usage_fields["prompt_cache_hit_tokens"] = usage.get("cache_read_input_tokens")
                    if usage_fields.get("prompt_cache_miss_tokens") is None:
                        usage_fields["prompt_cache_miss_tokens"] = usage.get("cache_creation_input_tokens")
                    if usage_fields.get("prompt_tokens") is None:
                        usage_fields["prompt_tokens"] = usage.get("input_tokens")
                    if usage_fields.get("completion_tokens") is None:
                        usage_fields["completion_tokens"] = usage.get("output_tokens")

                if content or tool_calls:
                    return {
                        "success": True,
                        "response": content,
                        "reasoning": "",
                        "model": model_config.model_name,
                        "tool_calls": tool_calls,
                        **usage_fields,
                    }
            else:
                message = payload["choices"][0]["message"]
                content = str(message.get("content") or "").strip()
                tool_calls = message.get("tool_calls") or []
                if content or (isinstance(tool_calls, list) and tool_calls):
                    return {
                        "success": True,
                        "response": content,
                        "reasoning": "",
                        "model": model_config.model_name,
                        "tool_calls": tool_calls if isinstance(tool_calls, list) else [],
                        **usage_fields,
                    }
        except (KeyError, IndexError, TypeError) as exc:
            self.ctx.logger.warning(
                "外部接口返回结构异常 provider=%s err=%s payload_summary=%s",
                model_config.provider,
                exc,
                self._safe_summary(payload),
            )
            return {
                "success": False,
                "response": "",
                "reasoning": f"外部接口返回结构异常：{exc}",
                "model": model_config.model_name,
            }

        self.ctx.logger.warning(
            "外部接口返回内容为空 provider=%s payload_summary=%s",
            model_config.provider,
            self._safe_summary(payload),
        )
        return {
            "success": False,
            "response": "",
            "reasoning": "外部接口返回内容为空",
            "model": model_config.model_name,
        }

    @staticmethod
    def _safe_summary(value: Any, *, max_len: int = 200) -> str:
        """生成可安全回显/写日志的短摘要，避免把完整 API payload 打到聊天"""

        text = repr(value)
        if len(text) > max_len:
            return text[: max_len - 3] + "..."
        return text

    def _resolve_requirement_text(self, requirement: str) -> str:
        """把用户要求规范成尽量稳定的任务文案。

        短关键词映射到固定模板，减少无意义措辞差异；长句原样保留。
        """

        raw = str(requirement or "").strip()
        if not raw:
            raw = self.config.read.default_requirement

        if not self.config.read.use_requirement_templates:
            return raw

        # 仅对「较短、像口令」的要求做模板映射，避免误伤长指令
        compact = "".join(raw.lower().split())
        if len(raw) <= 24:
            for keywords, template in _REQUIREMENT_TEMPLATES:
                for keyword in keywords:
                    if keyword.lower() in compact or keyword in raw:
                        return template
        return raw

    def _build_system_instruction(self) -> str:
        """构造固定 system 说明（内置 + 可选自定义追加）。"""

        extra = str(self.config.read.fixed_system_instruction or "").strip()
        if extra:
            return f"{_FIXED_SYSTEM_INSTRUCTION}\n{extra}"
        return _FIXED_SYSTEM_INSTRUCTION

    def _build_prompt_messages(
        self,
        requirement: str,
        text_content: str,
        truncated: bool,
    ) -> list[dict[str, str]]:
        """构造利于前缀缓存的 messages。

        顺序固定为：
        1. system：稳定规则
        2. user：文件正文（同一文件重复处理时可复用）
        3. user：本次要求（易变，垫底）
        """

        resolved_requirement = self._resolve_requirement_text(requirement)
        truncated_note = "注意：文件较长，以下仅为截断后的前半部分。\n" if truncated else ""
        file_user_content = (
            f"【文件正文】\n{truncated_note}----------\n{text_content}\n----------"
        )
        requirement_user_content = (
            f"【本次要求】\n{resolved_requirement}\n\n请根据文件正文完成上述要求，直接给出结果。"
        )
        return [
            {"role": "system", "content": self._build_system_instruction()},
            {"role": "user", "content": file_user_content},
            {"role": "user", "content": requirement_user_content},
        ]

    @staticmethod
    def _split_prompt_for_providers(
        prompt: str | list[dict[str, str]],
    ) -> tuple[list[dict[str, str]], str, list[dict[str, str]]]:
        """把统一 prompt 拆成 OpenAI messages / Anthropic system+messages。"""

        if isinstance(prompt, str):
            openai_messages = [{"role": "user", "content": prompt}]
            return openai_messages, "", [{"role": "user", "content": prompt}]

        openai_messages: list[dict[str, str]] = []
        anthropic_messages: list[dict[str, str]] = []
        system_parts: list[str] = []
        for item in prompt:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user").strip().lower() or "user"
            content = str(item.get("content") or "")
            if role == "system":
                system_parts.append(content)
                openai_messages.append({"role": "system", "content": content})
                continue
            if role not in {"user", "assistant"}:
                role = "user"
            openai_messages.append({"role": role, "content": content})
            anthropic_messages.append({"role": role, "content": content})

        if not openai_messages:
            openai_messages = [{"role": "user", "content": ""}]
        if not anthropic_messages:
            # Anthropic 不允许空 messages；若只有 system，补一条空 user
            anthropic_messages = [{"role": "user", "content": "请根据 system 说明处理。"}]
        return openai_messages, "\n".join(part for part in system_parts if part).strip(), anthropic_messages

    def _build_prompt(self, requirement: str, text_content: str, truncated: bool) -> str:
        """兼容旧接口：把 cache 友好 messages 压成单字符串。"""

        messages = self._build_prompt_messages(requirement, text_content, truncated)
        parts = [f"[{m['role']}]\n{m['content']}" for m in messages]
        return "\n\n".join(parts)


def create_plugin() -> FileReaderPlugin:
    """创建文件阅读插件实例"""

    return FileReaderPlugin()
