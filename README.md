# 文件阅读插件

> 用户通过指令指定文件（NapCat `file_id` 或 `http(s)` 链接），或**引用一条文件消息**，插件按**纯文本**读取后交给 LLM 处理并回复。

- **插件 ID**：`github.xiexiaojia780.file-reader-plugin`
- **版本**：1.2.9
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
| `http://` / `https://` | 插件直接下载（有大小上限；**默认拦截私有/本地地址防 SSRF**） |
| 引用/回复文件消息 | `get_msg` 取原消息 → 抽 `file_id`/`url` → 再 `get_file` 或直链下载 |
| 手填 `file_id` | 调 OneBot **HTTP** `POST {http_base_url}/get_file`（**不走 WebSocket**），按 `base64` → `url` → 本地 `file`/`path` 兜底 |

本地路径读取受 `napcat.allowed_local_prefixes` 白名单约束；留空则**禁止**本地路径，只接受 base64/url。  
默认白名单仅临时目录（`/tmp`、`/var/tmp`、`C:\Windows\Temp`），**不要**轻易加入 `C:\Users` 等过宽前缀。

#### 安全说明

- **用户 URL SSRF**：`/读文件 https://...` 会在下载前解析主机并拒绝私有/回环/链路本地/云元数据等地址；重定向每一跳也会重新校验。可用 `read.block_private_urls=false` 关闭（仅可信环境），或用 `read.url_allowed_hosts` 限制允许的主机名。
- **NapCat `get_file`**：因返回体可能较大，当前仍走裸 HTTP，不经 SDK `api.call`。`http_base_url` 应只指向**可信本机** NapCat；其返回的 `url` 兜底下载不套用用户侧私有地址拦截（常见为 127.0.0.1 缓存地址）。若后续 SDK 提供文件下载通道，应优先迁移。

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
config_version = "1.2.9"

[napcat]
# 仅 HTTP；本插件不通过 WebSocket 取文件。端口改成你 NapCat 实际 HTTP 端口。
# get_file 走裸 HTTP（不经 SDK），请只指向可信本机 NapCat。
http_base_url = "http://127.0.0.1:3000"
access_token = ""
# 允许读取本地路径的目录前缀，逗号分隔；留空则禁止本地路径
# 默认仅临时目录；请按实际环境收紧，不要写 C:\Users 这类过宽前缀
allowed_local_prefixes = "/tmp,/var/tmp,C:\\Windows\\Temp"

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
block_private_urls = true          # 拦截用户私有/本地 URL（防 SSRF）
url_allowed_hosts = ""             # 可选主机白名单，如 cdn.example.com

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

## 故障排查

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| 发了 `/读文件` 没反应 | 插件未启用；命令被权限拦截且 `silent_deny=true` | 检查 WebUI 插件开关、`[plugin].enabled`、`[access]` |
| 提示无权限 | `access_mode=whitelist/blacklist` 未放行当前用户 | 核对 `user_whitelist` / `user_blacklist`（支持 `123456` 或 `qq:123456`） |
| 引用文件后提示获取失败 | napcat-adapter 未加载，或 `get_msg` 不可用 | 确认适配器已加载；看插件日志里 `get_msg` / API 调用错误 |
| `file_id` 取文件失败 | NapCat **HTTP** 未开，或 `http_base_url` 端口不对 | 在 NapCat 开启 HTTP 服务，端口与配置一致（勿照抄示例 `3000`） |
| 本地路径被拒绝 | 路径不在 `allowed_local_prefixes`，或白名单为空 | 按实际 NapCat 缓存目录追加前缀；**不要**直接写 `C:\Users` 过宽路径 |
| 私有/内网 URL 被拒绝 | 默认 SSRF 防护（`block_private_urls=true`） | 改用公网 URL；仅可信环境可关拦截，或配置 `url_allowed_hosts` |
| 提示仅支持纯文本 / 内容为空 | PDF、Office、图片等二进制 | 换成 `.txt` / `.md` / `.json` / 源码等纯文本 |
| external 模式直接中止 | 未配置 `api_key` | 填写 `[model].api_key`，或将 `mode` 改为 `host` |
| LLM 处理失败 | 宿主任务名错误 / 外部 API 不可达 | host 检查 `task_name`（任务名不是模型 ID）；external 检查 `api_base_url`、密钥与网络；细节看日志 |
| 处理完后 bot 又接一句闲聊 | 旧版本未关入库 | 使用 ≥1.2.3；本插件回复默认 `storage_message=False` 并拦截主链 |

更多细节也可对照上文「取文件方式」「安全说明」「限制说明」。

## 测试

不依赖真实 Host / 网络的 mock 测试：

```bash
cd file_reader_plugin
python test_model_dispatch.py
```

## 版本记录

### 1.2.9

- **SSRF 防护**：用户提供的 `http(s)` URL 默认拦截私有/本地/链路本地/云元数据等地址；重定向每跳重新校验
- 新增配置 `read.block_private_urls`（默认 true）、`read.url_allowed_hosts`（可选主机白名单）
- NapCat `get_file` 返回的 `url` 兜底下载不套用用户侧私有地址拦截（本机缓存常见场景）
- 收紧默认 `allowed_local_prefixes`：移除 `C:\Users`，仅保留临时目录；README/config 补充安全说明
- 文档标明 `get_file` 走裸 HTTP、不经 SDK 的原因与后续迁移建议

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
