# 康康图（astrbot_plugin_kkt）

AstrBot 图片生成与编辑插件。通过 OpenAI 兼容接口调用图像模型，支持文生图、图生图、多图参考、引用消息、固定 `/hajimi` 和 `/kkt` 命令，以及群聊黑名单。

当前默认接口为：

```text
https://newapi.qianqianye.com/v1/chat/completions
```

## 功能

- 文生图：只提供文字描述即可生成图片。
- 图生图：发送图片并附带提示词，或回复图片后编辑。
- 引用图文：引用消息中的文字会作为 Prompt，引用图片会作为参考图。
- 多图输入：引用图片和当前消息图片会合并发送，并自动去重。
- 忽略引用产生的 @：引用消息时平台自动附带的 @ 不会进入 Prompt，也不会自动触发头像参考图。
- 固定命令：`/hajimi` 和 `/kkt`，两者都可以触发插件。
- 群聊黑名单：黑名单群不会响应，也不会调用图像 API。
- 异步请求、失败重试和临时文件自动清理。
- Key 轮询：主 Key 失败后按顺序切换备用 Key（`/hajimi` `/kkt` 与 `/image2` 各自独立）。
- `/image2` 默认走 OpenAI Images API：文生图 `/images/generations`，有参考图时 `/images/edits`（与 `/kkt` 的 chat 路径隔离）。

## 安装

将插件目录放入 AstrBot 插件目录：

```text
AstrBot/data/plugins/astrbot_plugin_kkt/
```

插件目录中应包含：

```text
astrbot_plugin_kkt/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
└── README.md
```

安装依赖后，在 AstrBot WebUI 中重载插件，或重启 AstrBot。

## 快速配置

首次使用前，在 AstrBot WebUI 的插件配置中填写：

```text
api_key = 你的 NewAPI 主 API Key
```

可选：填写备用 Key 列表，主 Key 失败后自动轮询：

```text
backup_api_keys = ["备用Key1", "备用Key2"]
```

`/image2` 通道独立配置：

```text
image2_api_key = image2 主 Key
image2_backup_api_keys = ["image2备用Key1"]
image2_api_mode = images          # images | chat | auto
image2_model = gpt-image-2
image2_size = 1024x1024
```

说明：

- `image2_api_mode=images`（默认）：无参考图 → `/v1/images/generations`；有参考图 → `/v1/images/edits`（仅 1 张）
- **多参考图硬拦截**（images/auto→images）：收到 ≥2 张参考图时直接提示用法，**不请求上游、不扣日配额、不记 CD**
- `image2_api_mode=chat`：与 `/kkt` 一样走 `chat/completions`（仅当你的 image2 模型支持 chat 出图时；多图不拦截）
- `/hajimi` `/kkt` **始终**走 `chat/completions`，不受 `image2_api_mode` 影响

默认配置：

```text
api_base = https://newapi.qianqianye.com/v1
model = gemini-3.1-flash-image
temperature = 0.7
```

也可以通过环境变量提供主 API Key：

```text
NEW_API_KEY
```

插件配置中的 `api_key` 优先级更高。日志里只会打印 Key 掩码（如 `sk-0gb...YsQh`），不会输出完整密钥。

### Key 轮询策略

1. 先用主 Key（`api_key` 或 `image2_api_key`）。
2. 同一 Key 内按 `max_retry` 重试网络错误 / 429 / 5xx。
3. 当前 Key 仍失败（含 401/403/429/5xx、上游 no channel）时，切换下一个备用 Key。
4. 模型明确用文字拒答时不再切换 Key（避免无意义消耗）。
5. 所有 Key 都失败后，把最后一次错误返回给用户。

### 分通道日配额与预估费用（仅管理员）

计费桶：

- `main`：`/kkt` + `/hajimi` 共用
- `image2`：仅 `/image2`

成功出图后才记 1 次（失败不扣）。持久化 `data/plugin_data/kkt/usage.json`（含今日 `daily` 与累计 `total`）。

WebUI 可配置：

```text
daily_quota_main / daily_quota_image2   # 各通道日上限，0=不限制
cost_main_usd / cost_image2_usd         # 预估 USD/次（仅展示）
```

指令（**仅管理员**）：

```text
/kkt额度                     # 查询：分通道今日/累计次数 + 预估费用
/kkt额度 10                  # 两通道日上限都改为 10
/kkt额度 main 100            # 只改 main
/kkt额度 image2 20           # 只改 image2
/kkt额度 0                   # 两通道关闭日限额
/kkt重置额度                 # 清零全部通道今日已用（累计 total 保留）
/kkt重置额度 main            # 只清 main 今日
/kkt统计                     # 同 /kkt额度 查询
```

费用 = 当前配置单价 × 次数（预估，非上游账单；改单价后展示按新价重算）。  
运行时限额写入 `channel_quota_limit.json`；不会改 WebUI 配置文件。

## 指令格式

默认情况下，唤醒词必须带 `/`，不使用 `#` 前缀：

```text
/hajimi <提示词>
```

默认主唤醒词为：

```text
hajimi
```

### 文生图

只发送文字指令：

```text
/hajimi 一只穿宇航服的橘猫，站在火星表面
```

插件会将提示词发送给图像模型，并返回生成的图片。

### 帮助

```text
/hajimi帮助
/hajimi help
/hajimi ?
```

不带提示词时也会返回帮助内容：

```text
/hajimi
```

帮助内容会根据当前配置自动显示主唤醒词、别名、斜杠规则和图片使用方式。

## 图生图和多图用法

### 当前消息同时发送图片和文字

在同一条消息中发送一张图片和指令：

```text
/hajimi 改成水彩画风，保留人物主体
```

插件会将当前消息中的图片和文字一起发送给模型。

### 引用图片后编辑

先发送一张图片，然后引用这张图片发送：

```text
/hajimi 删除背景，换成纯白色背景
```

插件会读取引用图片，并使用当前指令作为编辑提示词。

### 引用文字后生成图片

引用一条纯文字消息，再发送：

```text
/hajimi
```

被引用的文字会作为 Prompt。引用消息自动附带的 `@原发送者` 会被忽略。

### 引用图文消息

如果被引用消息同时包含文字和图片：

```text
原消息：图片 + 把这个人物放到海边
```

再发送：

```text
/hajimi 改成夕阳效果
```

实际发送给模型的内容为：

```text
Prompt：
把这个人物放到海边
改成夕阳效果

参考图片：
原消息中的图片
```

引用文字会放在当前指令之前，引用产生的 `@` 不会进入 Prompt。

### 引用图片并附带当前新图片

例如：

```text
用户 A：图片 1
```

你引用图片 1，同时在当前消息中发送图片 2 和：

```text
/hajimi 把这两个人对换
```

插件会发送：

```text
Prompt：把这两个人对换
参考图片：图片 1 + 图片 2
```

引用图片和当前消息图片会合并处理，重复图片会自动去重。

如果两边都有多张图片，插件会将所有图片按“引用图片在前、当前消息图片在后”的顺序发送。

## @用户头像

默认不使用 `@用户` 头像：

```text
enable_at_avatar = false
```

开启后：

```text
enable_at_avatar = true
```

当当前消息和引用消息中都没有图片时，可以在指令中 @ 用户，将其头像作为参考图：

```text
@某用户 /hajimi 把头像改成赛博朋克风格
```

注意：

- 如果消息中已有图片，不会额外读取 @ 用户头像。
- 引用消息时平台自动带上的 @ 不会被当作头像目标。
- 是否能获取头像取决于当前适配器提供的消息组件和网络环境。

## 唤醒词和别名

### 固定命令

插件固定注册以下两个 AstrBot 命令：

```text
/hajimi 一只猫
/kkt 一只猫
```

帮助命令：

```text
/hajimi帮助
/kkt帮助
```

两个命令会出现在 AstrBot 的“管理行为”中。命令必须带 `/` 前缀。

## 群聊黑名单

在 WebUI 配置 `group_blacklist`，只填写纯数字群号：

```text
[123456789, 987654321]
```

黑名单群中的插件指令会被直接忽略：

- 不返回帮助。
- 不读取图片。
- 不调用 API。
- 不发送错误消息。

私聊不受群黑名单影响。

## 本地敏感词过滤（Sensitive-lexicon）

默认**关闭**。开启后对 `/kkt` `/hajimi` `/image2` 三通道的提示词做本地关键词拦截：命中则提示「内容审核未通过」，**不请求上游、不扣日配额**。用户侧不展示命中词；日志会记录 `category` 与 `keyword`。

### 下载词库（不随插件分发）

```bash
# 推荐放到插件数据目录
cd /path/to/AstrBot/data/plugin_data/kkt
git clone https://github.com/konsheng/Sensitive-lexicon.git
```

仓库：https://github.com/konsheng/Sensitive-lexicon  
需要目录内有 `Vocabulary/*.txt`（如 `政治类型.txt`）。

### 相关配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `sensitive_filter_enabled` | `false` | WebUI 默认总开关 |
| `sensitive_lexicon_path` | 空 | 空则用 `data/plugin_data/kkt/Sensitive-lexicon` |
| `sensitive_categories` | `[]` | 空=全部已映射类别；可多选如 `政治`、`暴恐`、`色情` |

### 指令开关（运行时，仅管理员可改）

```text
/kkt审核              # 查询 → 仅回复「本地审核：开/关」
/kkt审核 开           # 开启（仅管理员）
/kkt审核 关           # 关闭（仅管理员）
```

群聊**不展示**词条数/类别；详情只在服务端日志。开关写入 `data/plugin_data/kkt/sensitive_filter_enabled.json`，重载仍生效。

类别与文件对应见 WebUI 配置 hint。建议先只启用 `政治` 观察误杀，再按需加类。

## 全部配置项

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---:|---|
| `group_blacklist` | list | `[]` | 群聊黑名单，填写纯数字群号 |
| `api_base` | string | `https://newapi.qianqianye.com/v1` | API 基址，不要填写 `/chat/completions` |
| `api_key` | string | 空 | NewAPI API Key |
| `model` | string | `gemini-3.1-flash-image` | 图像模型名称 |
| `temperature` | float | `0.7` | API 请求参数 |
| `timeout` | int | `180` | API 超时时间，单位秒 |
| `max_retry` | int | `2` | 网络错误、429、5xx 的重试次数 |
| `retry_delay` | int | `2` | 重试间隔，单位秒 |
| `enable_reply_image` | bool | `true` | 是否读取引用消息中的图片 |
| `enable_at_avatar` | bool | `false` | 是否允许使用 @用户头像 |
| `cleanup_delay` | int | `15` | 图片发送后清理临时文件的延迟秒数 |
| `sensitive_filter_enabled` | bool | `false` | 本地敏感词过滤总开关 |
| `sensitive_lexicon_path` | string | 空 | Sensitive-lexicon 根目录 |
| `sensitive_categories` | list | `[]` | 启用类别（空=全部） |

## 处理规则总结

### Prompt 来源

```text
引用消息文字
    +
当前 /hajimi 或 /kkt 后面的文字
```

引用消息中的图片、当前消息中的图片和可选头像不会转换成文字，而是作为多模态图片输入发送。

### 图片来源优先级

```text
引用消息图片
    +
当前消息图片
    ↓
如果没有任何图片，并且 enable_at_avatar=true
    ↓
@用户头像
```

引用图片和当前图片会合并，不会因为存在引用图片而丢弃当前图片。

### 自动 @ 的处理

引用消息产生的 `@` 组件会被忽略：

- 不加入 Prompt。
- 不作为头像目标。
- 不影响图片收集。

## API 请求格式

插件发送的请求结构为 OpenAI 多模态格式：

```json
{
  "model": "gemini-3.1-flash-image",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "把这两个人对换"
        },
        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/jpeg;base64,..."
          }
        }
      ]
    }
  ],
  "temperature": 0.7
}
```

请求头：

```text
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

插件会兼容以下常见图片响应形式：

- `choices[0].message.images`
- `choices[0].message.content` 中的 `image_url`
- 文本中的 Data URL
- 文本中的普通图片 URL

## 性能和安全

- 群黑名单在指令解析、图片读取和 API 请求之前判断。
- 使用异步 `aiohttp`，避免阻塞 AstrBot 事件循环。
- 图片只在确定需要发送给模型时转换为 Base64。
- 图片按来源去重，避免重复上传相同图片。
- 图片结果保存在 AstrBot 数据目录，发送后自动清理。
- 启动时清理超过 1 小时的残留临时文件。
- 认证失败不重试，网络错误、429 和 5xx 按配置重试。
- API Key 不写入日志。

## 当前接口范围

当前版本使用：

```text
POST /v1/chat/completions
```

当前主要发送：

```text
model
messages
temperature
```

`size`、`quality`、`style`、`n`、`response_format` 等参数暂未默认发送。这些参数通常属于 `/images/generations` 或其他图像接口，后续可根据不同模型和接口协议扩展。
