# 文件阅读插件

> 用户通过指令指定文件（NapCat `file_id` 或 `http(s)` 链接），或**引用一条文件消息**，插件按**纯文本**读取后交给 LLM 处理并回复。

- **插件 ID**：`github.xiexiaojia780.file-reader-plugin`
- **版本**：1.2.8
- **作者**：[xiexiaojia780](https://github.com/xiexiaojia780)
- **仓库**：[https://github.com/xiexiaojia780/file_reader_plugin](https://github.com/xiexiaojia780/file_reader_plugin)
- **License**：`GPL-3.0-or-later`（与 `_manifest.json` / 根目录 `LICENSE` 一致）
- **指令**：`/读文件`（别名 `/readfile`）

## 仓库结构

```
file_reader_plugin/
├── _manifest.json          # 插件元数据与依赖声明
├── plugin.py               # 入口：配置模型 + 指令处理 + 取文件 + LLM
├── config.toml             # 默认配置示例
├── test_model_dispatch.py  # mock 测试（模型分派 / NapCat / 安全）
├── README.md
├── LICENSE                 # GPL-3.0-or-later
└── _locales/               # i18n
```

## 功能

### 指令：`/读文件` / `/readfile`

| 参数 | 必填 | 说明 |
|------|------|------|
| 文件标识 | 条件必填 | NapCat 的 `file_id`，或 `http(s)://` 直链；**引用文件消息时可省略** |
| 要求 | 否 | 对文件内容的处理要求；省略时用配置里的默认要求 |

示例：

```text
/读文件 https://example.com/notes.txt
/读文件 https://example.com/notes.txt 提取所有待办事项
/readfile ABC123DEF 用三句话概括
```

#### 引用文件消息（推荐）

1. 在 QQ 里**回复/引用**一条文件消息
2. 发送：

```text
/读文件
/读文件 概括一下
/读文件 提取所有待办事项
```

插件会从被引用消息中解析 `file_id` / `url`（通过 napcat-adapter 的 `get_msg`），再走原有下载与 LLM 流程。  
前提：napcat-adapter 已加载，且 OneBot HTTP 可调用 `get_file`。

### 取文件方式

| 来源 | 行为 |
|------|------|
| `http://` / `https://` | 插件直接下载（有大小上限） |
| 引用/回复文件消息 | `get_msg` 取原消息 → 抽 `file_id`/`url` → 再 `get_file` 或直链下载 |
| 手填 `file_id` | 调 OneBot **HTTP** `POST {http_base_url}/get_file`（**不走 WebSocket**），按 `base64` → `url` → 本地 `file`/`path` 兜底 |

本地路径读取受 `napcat.allowed_local_prefixes` 白名单约束；留空则**禁止**本地路径，只接受 base64/url。

### LLM 模式

| `model.mode` | 行为 |
|--------------|------|
| `host`（默认） | 使用宿主任务模型：`ctx.llm.generate(model=task_name)`。注意这里的 `model` 是**任务名**（如 `replyer`），不是具体模型 ID |
| `external` | 插件用 aiohttp 直调 OpenAI 兼容或 Anthropic 原生接口 |

`external` 时必须配置 `api_key`，否则指令会直接提示并中止。

### 限制说明

- **仅纯文本**：按文本解码读取；`.txt` / `.md` / `.json` / `.log` / 源码等可用。PDF、Word、Excel、图片等二进制内容**不会**被解析（通常会提示无法按文本读取）。
- **用户权限**（`[access]`，WebUI 里有「访问模式」下拉框）：
  - `access_mode=all`：所有人可用（默认，名单可留空）
  - `access_mode=whitelist`：仅白名单用户可用 → 填 `user_whitelist`
  - `access_mode=blacklist`：黑名单用户禁用，其他人可用 → 填 `user_blacklist`
  - ID 写法：`123456` 或 `qq:123456`，逗号/换行分隔
- 超过 `read.max_chars` 会截断，并在 prompt 中注明「仅为前部分」。
- 下载/读取超过 `read.max_download_bytes` 会报错。

## 安装

1. 放到 MaiBot 的 `plugins/` 目录。
2. 重启 Maibot 或等待其热重载。
3. 依赖由 `_manifest.json` 自动安装；也可手动：

```bash
pip install "aiohttp>=3.8" "charset-normalizer>=3.0"
```

4. 若要用 NapCat `file_id`：在 NapCat 开启 **HTTP服务器**，并填写下方 `[napcat]` 配置。

> **WebSocket 说明：** 本插件取文件只走 HTTP（`POST {http_base_url}/get_file`），**不需要单独为它再开 WebSocket**。  
> WebSocket 是 MaiBot / napcat-adapter 收发 QQ 消息用的；机器人能正常聊天时通常已经开着，与 file_reader 的 `file_id` 取文件无关。  
> 只用 URL（`/读文件 https://...`）时，甚至不必依赖 NapCat HTTP。  
> `http_base_url` 的端口必须与 NapCat 实际 HTTP 服务一致（不要照抄示例 `3000`；以你本机 NapCat 配置为准）。

## 配置

```toml
[plugin]
enabled = true
config_version = "1.2.8"

[napcat]
# 仅 HTTP；本插件不通过 WebSocket 取文件。端口改成你 NapCat 实际 HTTP 端口。
http_base_url = "http://127.0.0.1:3000"
access_token = ""
# 允许读取本地路径的目录前缀，逗号分隔；留空则禁止本地路径
allowed_local_prefixes = "/tmp,/var/tmp,C:\\Windows\\Temp,C:\\Users"

[access]
access_mode = "all"                 # all | whitelist | blacklist（WebUI 下拉）
user_whitelist = ""                 # whitelist 模式用，如 "123456,qq:789"
user_blacklist = ""                 # blacklist 模式用
deny_message = "你没有权限使用文件阅读功能。"
silent_deny = false                 # true=拒绝时不说话

[read]
max_chars = 20000
max_download_bytes = 5000000
timeout_seconds = 30
default_requirement = "概括这个文件的主要内容"
fixed_system_instruction = ""      # 可选自定义固定说明
use_requirement_templates = true   # 短口令映射稳定任务模板
use_fixed_tools = true             # 固定 tools 定义，利于前缀稳定
report_cache_stats = true          # 日志输出 cache 命中率
report_cache_stats_to_chat = false # true 时聊天里追加 [cache] 统计

[model]
mode = "host"                 # host | external
task_name = ""                # host：replyer / planner / utils 等，空=默认
provider = "openai"           # external：openai | anthropic
api_base_url = "https://api.openai.com/v1"
api_key = ""
model_name = "gpt-4o-mini"
temperature = 0.7
max_tokens = 2048
```

也可在 WebUI 的「插件 / NapCat 连接 / 读取与处理 / 模型选择」分组中修改。

### external 模式提示

| provider | `api_base_url` 示例 | 实际请求 |
|----------|---------------------|----------|
| `openai` | `https://api.openai.com/v1` 或中转站 `/v1` | `POST .../chat/completions` + Bearer |
| `anthropic` | `https://api.anthropic.com` 或 `.../v1` | `POST .../v1/messages` + `x-api-key`（不会拼出 `/v1/v1`） |

## 使用效果（聊天侧）

1. 用户发送指令后，插件先取文件；失败则回复 `获取文件失败：...`。
2. 若不是可读文本，回复说明「仅支持纯文本」。
3. 成功读取后先发一条：`已读取文件，正在处理……`。
4. LLM 完成后，把处理结果作为普通文本发回当前聊天。
5. LLM 失败时回复精简错误信息（完整细节写日志，避免把 API payload 打到群里）。

## 测试

不依赖真实 Host / 网络的 mock 测试：

```bash
cd file_reader_plugin
python test_model_dispatch.py
```

## 版本记录

### 1.2.8

- 用户权限改为**下拉选项**：`access_mode = all / whitelist / blacklist`
- WebUI 字段补充中文 label/hint，更方便点选
- 移除文件扩展名黑白名单（`extension_whitelist` / `extension_blacklist` / `reject_unknown_extension`）；仍按纯文本解码，二进制文件会提示无法读取

### 1.2.7

- 新增**用户**黑白名单：`access.user_whitelist` / `user_blacklist` / `deny_message` / `silent_deny`
- 支持 `qq:123456` 与纯数字 ID

### 1.2.5

- 处理完成后输出 **prompt cache 命中率**（日志默认开；聊天可选）
- 兼容 host 的 `prompt_cache_hit_tokens` 与 external 的 `usage` / `cached_tokens` / Anthropic `cache_read_input_tokens`

### 1.2.4

- LLM 请求改为 **固定 system + 固定 tools + 文件 + 要求**
- host 走 `generate_with_tools`；external 在 OpenAI/Anthropic 请求体中附带 tools
- 支持从 `submit_file_result` / `report_file_limitation` tool_calls 提取最终文本
- 配置项 `read.use_fixed_tools`（默认 true）

### 1.2.3

- 修复处理完后 Maisaka 继续闲聊：命令回复 `storage_message=False`，不入库/不进 history
- 命令返回拦截等级改为 `2`，确保跳过 HeartFlow/Maisaka 主链

### 1.2.2

- Prompt 改为 cache 友好结构：`system(固定说明)` → `user(文件正文)` → `user(本次要求)`
- 常见短要求（概括/待办/讲代码/翻译）映射为稳定任务模板
- host / external（OpenAI & Anthropic）均走 messages 列表

### 1.2.1

- 修复引用消息时命令不触发：Host 匹配的是带 `[回复了...]` 前缀的 `processed_plain_text`，命令正则不再使用 `^` 行首锚定

### 1.2.0

- 支持**引用文件消息**后发 `/读文件 [要求]`，自动解析 `file_id`/`url`
- 命令参数 `file_ref` 可省略（有引用或本条文件上下文时）
- 声明 `api.call` 能力，调用 napcat-adapter `get_msg`

### 1.1.0

- 补齐 `config.toml` / `README` / `LICENSE` / `.gitignore` / i18n 文案
- external 模式校验 `api_key`；失败信息不再回显完整 payload
- NapCat 本地路径白名单；base64 也受大小上限约束
- `on_config_update` / 卸载日志；纯文本能力说明更明确
- 扩展 mock 测试

### 1.0.0

- 初版：`/读文件` 读取 URL 或 NapCat `file_id` 并交 LLM 处理
