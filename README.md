# 康康图（astrbot_plugin_kkt）

AstrBot 多通道媒体插件，支持 OpenAI 兼容生图/修图、Grok Images API、Grok 2K 文生图、GIF 分镜，以及 grok2api 异步视频。插件包含 `KKT Studio` WebUI 控制台，用于连接测试、运行状态查看和常用参数管理。

当前版本：`0.18.4`

## 功能总览

- `/hajimi`、`/kkt`：主图像通道，支持文生图、多图参考、引用图和 @头像。
- `/image2`：独立 Image2 通道，可配置独立基址、协议、模型和 Key。
- `/grok`、`/gk`：使用 `grok-imagine-image-quality`，通过 grok2api Images API 生图/多图编辑。
- `/grok2`、`/grok2k`、`/gk2`、`/gk2k`：Grok 2K 文生图，不接受参考图。
- `/grokvideo`、`/grokv`、`/gkv`、`/gv`：grok2api 文生视频/单图生视频。
- GIF 分镜：`/hajimigif`、`/kktgif`、`/hajimigif2`、`/image2gif`、`/image2gif2`。
- `/kkgif`：把当前消息或回复中的一个视频本地转换为 GIF，不调用模型。
- `/kkgifzip`、`/gifz`、`/gifzip`（可配别名）：五档压缩视频或 GIF（本地 FFmpeg）；静态图不支持。
- 引用消息文字自动合并到提示词，引用图片自动作为参考图。
- 多图按“引用图 → 当前消息图片 → @头像”顺序收集，并自动去重。
- GIF/WebP 参考图可在后台选择首帧、中间帧或末帧。
- 视频任务有全局并发、每用户并发、用户冷却和日额度保护。
- 每个主行为都支持多个自定义别名，默认别名全部保留；视频别名支持末尾附加时长。
- 视频 Prompt 增强、视频额外提示词、中文文字和轻量本地化约束均可独立配置。
- 失败重试、Key 轮询、敏感词前置拦截和临时文件清理。
- QQ/NapCat 视频发送前自动转成更兼容的 H.264 + AAC MP4。

## 安装

将插件目录放入：

```text
AstrBot/data/plugins/astrbot_plugin_kkt/
```

目录主要文件：

```text
astrbot_plugin_kkt/
├── main.py
├── video_client.py
├── web_api.py
├── metadata.yaml
├── _conf_schema.json
├── pages/dashboard/
│   ├── index.html
│   ├── app.js
│   └── style.css
├── requirements.txt
└── README.md
```

依赖：

- `aiohttp`
- `Pillow`
- 系统命令 `ffmpeg`、`ffprobe`（视频发送转码需要）

安装依赖后，在 AstrBot WebUI 重载插件即可，不需要重启机器人。

## KKT Studio WebUI

在 AstrBot 插件管理中打开 **KKT Studio 控制台**。

控制台提供：

- 主图像、Image2、Grok 生图、Grok 视频四条通道的地址和 Key 状态；两个 Grok 通道相邻显示。
- 四个主行为以及 GIF 分镜的自定义别名列表，页面会列出 canonical 指令和全部别名。
- 连接测试：只请求 `/healthz` 和 `/v1/models`，不会创建图片或视频任务。
- 视频并发占用、全局上限、每用户上限、冷却和清理延迟。
- 视频默认时长、宽高比、分辨率、轮询间隔、超时、Prompt 增强和额外提示词。
- 动图参考帧选择：首帧/中间帧/末帧。
- 主通道、Image2、视频的今日额度和累计次数。
- 通用重试、图片输入、中文软约束、消息回应、GIF、群黑名单和本地审核配置。
- 最近任务文本日志：Prompt、模型、通道、状态、进度、开始/结束时间、耗时和请求 ID；不保存图片。
- 所有普通配置项统一保存。保存后需要重载插件才会应用到运行实例。

页面使用 AstrBot Plugin Page bridge，不直接访问插件 API，也不把完整 Key 显示在页面上。若页面提示桥接 SDK 未加载，请从 AstrBot 插件管理中的页面入口重新打开，不要直接打开 HTML 文件。

静态配置仍可在 AstrBot 原生配置面板编辑；WebUI 与 `_conf_schema.json` 使用同一套字段，普通配置不会只存在于 Page 页面。

## 首次配置

### 主图像通道

`主图像通道（/hajimi /kkt）`：

```text
api_base = https://your-openai-compatible-host/v1
api_key = your-main-key
model = your-image-model
```

### Grok 通道

推荐先填写 Grok 生图通道。视频通道的地址、Key 和备用 Key 留空时，会逐项复用 Grok 生图通道：

```text
grok_api_base = https://g2a.example.com
grok_api_key = g2a_xxx
grok_backup_api_keys = g2a_backup_1, g2a_backup_2
```

视频需要单独覆盖时再填写：

```text
video_api_base = https://video.example.com
video_api_key = g2a_video_xxx
video_backup_api_keys = g2a_video_backup
```

地址可以写根地址或带 `/v1`，不要写完整接口路径。Grok 生图留空时回退主 `api_base`/`api_key`；视频未填写的字段回退 Grok 生图对应字段。

### Image2 通道

Image2 使用独立 Key：

```text
image2_api_key = image2-key
image2_model = gpt-image-2
image2_api_base = https://your-host/v1
image2_api_mode = images
image2_size = 1024x1024
```

`image2_api_key` 不会回退到主 `api_key`。

### 配置同步与视频 Prompt

普通配置面板和 KKT Studio 使用同一套配置字段。除了通道地址/Key 外，还包括：

- `video_prompt_enhance`：默认开启，为视频请求追加主体一致、动作连贯、镜头和时序约束。
- `video_style_prompt`：视频专用额外提示词，与图片 `style_prompt` 分开。
- `video_duration`、`video_aspect_ratio`、`video_resolution`、轮询/超时、并发、冷却和清理延迟。
- `enable_reply_image`、`enable_at_avatar`、`animated_reference_frame`、多图标签、中文软约束和回应表情。
- 分通道日额度、预估单价、GIF/视频转 GIF 参数、群黑名单和本地敏感词审核。
- `*_command_aliases`：每个主行为可配置多个别名；默认别名自动保留。

WebUI 只显示 Key 是否配置及备用 Key 数量，不回显密钥。填写新 Key 后保存并重载插件；留空不会覆盖已保存的 Key。

## 指令清单

行为表中的 canonical 主指令固定为 `/hajimi`、`/image2`、`/grok`、`/grok2` 和 `/grokvideo`；插件配置页面会同时列出当前生效的全部别名。默认别名不会因为填写自定义列表而消失。

别名配置项如下，均支持填写多个值（逗号或换行分隔）：

```text
main_command_aliases = kkt
image2_command_aliases =
grok_command_aliases = gk
grok2_command_aliases = grok2k, gk2, gk2k
video_command_aliases = grokv, gkv, gv
```

别名只能是单个指令 token；与其他主指令冲突的值会被忽略并写入日志。视频别名还支持 `/别名5` 形式。

`/kkt帮助` 使用两节点合并转发：第一节点是基础操作和参数，第二节点是当前生效的别名列表。平台不支持合并转发时自动降级为两段普通文本。

### 普通生图

```text
/hajimi 一只穿宇航服的橘猫站在火星
/kkt 一只穿宇航服的橘猫站在火星
```

`/hajimi` 和 `/kkt` 使用同一个 `main` 额度桶。

### Grok 生图

```text
/grok 一只猫坐在窗边
/gk 一只猫坐在窗边
```

Grok 支持：

- 纯文生图
- 当前消息附图
- 回复图片后编辑
- 多张参考图
- @用户头像作为参考图（需开启 `enable_at_avatar`）

### Grok 2K 文生图

```text
/grok2 一座雨夜里的未来城市
/grok2k 一座雨夜里的未来城市
/gk2 一座雨夜里的未来城市
/gk2k 一座雨夜里的未来城市
```

`/grok2` 固定请求 `resolution=2k`，只支持文生图。

如果消息中存在附图、回复图或 @头像，插件会在本地直接拒绝，不请求上游：

```text
/grok2 是 2K 文生图模式，不支持参考图。请使用 /grok 进行图生图。
```

原因是当前 grok2api Web 图片编辑接口只接受 `resolution=1k`。

### Grok 视频

```text
/grokvideo 一只猫在雨中奔跑
/grokv 一只猫在雨中奔跑
/gkv 一只猫在雨中奔跑
/gv 一只猫在雨中奔跑
```

指定本次时长：

```text
/grokvideo 5 一只猫在雨中奔跑
/grokvideo5 一只猫在雨中奔跑
/grokv5 一只猫在雨中奔跑
```

时长范围是 `1-15` 秒，未指定时使用后台 `video_duration`。

图生视频：

```text
（附一张图片）/grokvideo 让主体挥手
（回复一张图片）/grokvideo 让镜头慢慢推进
```

视频每次最多一张参考图作为首帧。多张图片会直接拒绝，不请求上游。

视频流程：

1. 并发检查通过后发送一次猫娘化等待提示。
2. 创建异步任务并后台轮询。
3. 完成后下载视频并转成 QQ 兼容格式。
4. 视频真正发送成功后，再发送生成成功和耗时文案。

生成期间不会持续发送百分比进度消息。

### GIF 分镜

```text
/hajimigif 让主角挥手
/kktgif 把主角做成表情包跳舞
/hajimigif2 让主角眨眼
/image2gif 让主角挥手
/image2gif2 让主角眨眼
```

这些命令是“先生图分镜，再裁切 GIF”，不是视频转 GIF。

### 视频转 GIF

引用一个视频或在当前消息附带一个视频，然后发送：

```text
/kkgif
```

规则：

- 每次只能处理一个视频。
- 视频最长 16 秒，超过会拒绝，不会静默截断。
- 默认首选最长边 480px、10 FPS、256 色调色板。
- 输出超过大小上限时自动降级到 360px/8 FPS，再降到 256px/8 FPS。
- GIF 不包含声音，发送为 GIF 图片。
- 转换在本机通过 FFmpeg 完成，不调用图片或视频模型。

### GIF 压缩

引用一个视频或 GIF，或在当前消息附带后发送：

```text
/kkgifzip
/gifz
/gifzip3
/kkgifzip5
```

规则：

- 裸指令 = 1 档；也可 `/指令3`（1-5）；数字越大压得越狠。
- 默认别名：`gifz`、`gifzip`（配置项 `kkgifzip_aliases`，与其它指令别名同样可改）。
- 支持：一个视频，或一个 GIF；不支持静态图。
- 视频最长 16 秒；约 10 FPS；纯本地 FFmpeg。
- 减灰：弱模糊 + 提饱和 + 更多色 + `stats_mode=diff`。

档位：

| 档 | 边长 | 色数 | crush |
|----|------|------|-------|
| 1 | 220 | 192 | ×2.0 |
| 2 | 180 | 160 | ×2.4 |
| 3 | 150 | 128 | ×2.8 |
| 4 | 120 | 96 | ×3.2 |
| 5 | 100 | 72 | ×3.8 |

输出受 `video_gif_max_bytes` 限制；过大时再砍分辨率/色数，不降 fps。

对应后台配置：

```text
video_gif_max_duration = 16
video_gif_max_dimension = 480
video_gif_fps = 10
video_gif_max_bytes = 8388608
```

默认参数：

- 4x4 分镜：16 帧
- 3x3 分镜：9 帧
- 单帧：256x256
- 播放速度：8 FPS
- 最大文件：8MB

## 图片输入规则

图片来源顺序：

```text
引用消息中的图片
当前消息中的图片
@头像（仅无其他图片且 enable_at_avatar=true 时）
```

引用图和当前消息图片会合并，不会因为有引用图而丢掉当前图片。

引用消息文字会加入 Prompt，引用自动产生的 @不会加入 Prompt，也不会自动作为头像目标。

### 动图参考帧

后台配置项：

```text
animated_reference_frame = 首帧
```

可选：

- `首帧`：第 1 帧，默认值，适合作为视频首帧。
- `中间帧`：时间中点附近的帧，适合主体在中途才出现的动图。
- `末帧`：最后一帧，适合使用动作完成状态。

插件会把 GIF/WebP 抽成普通 PNG，再发送给模型，并在开始生成提示中告知实际帧位置。

## 额度与并发

额度桶：

- `main`：`/hajimi`、`/kkt`
- `image2`：`/image2`
- `video`：`/grokvideo`、`/grokv`、`/gkv`、`/gv`

失败请求不计成功次数，成功生成后才记账。

管理员命令：

```text
/kkt额度
/kkt额度 main 100
/kkt额度 image2 20
/kkt额度 video 5
/kkt重置额度
/kkt重置额度 video
```

视频并发由后台配置控制：

```text
video_max_concurrent = 2
video_max_concurrent_per_user = 1
video_cooldown_seconds = 60
```

全局并发满时直接拒绝，不建立无限等待队列。

## 敏感词审核

默认关闭。开启后适用于各生图和视频提示词：

```text
sensitive_filter_enabled = true
```

命中时：

- 不请求上游。
- 不扣额度。
- 用户只看到通用审核提示。
- 具体类别和关键词只写入服务端日志。

管理员命令：

```text
/kkt审核
/kkt审核 开
/kkt审核 关
```

## 失败信息与日志

用户侧只显示简洁错误，例如：

```text
图片生成失败，请稍后重试。
视频生成失败，请稍后重试。
视频已生成，但发送失败，请稍后重试。
```

上游 HTTP 状态、请求阶段、Key 掩码、任务 ID 和详细异常只写入 AstrBot 日志，不直接展示给用户。

关键日志前缀：

```text
[kkt]
[kkt][webui]
```

## Grok2API 接口

Grok 生图：

```text
POST /v1/images/generations
POST /v1/images/edits
```

`/grok` 有参考图时向 `images` 数组提交多张图片；`/grok2` 无参考图时提交 `resolution=2k`。

Grok 视频：

```text
POST /v1/videos/generations
GET  /v1/videos/{request_id}
GET  /v1/videos/{request_id}/content
```

视频完成响应中的内网 `127.0.0.1` 媒体地址会自动改写为配置的 API 地址。

## 常见问题

### WebUI 改了参数但没生效

保存后在插件管理中重载 `astrbot_plugin_kkt`。连接和生成参数不会要求重启 AstrBot。

### `/grok2` 带图失败

这是预期行为。`/grok2` 只做 2K 文生图，带图请使用 `/grok`。

### 视频生成成功但 QQ 不显示

插件会在发送前转码为 H.264 + AAC + faststart MP4；如果仍失败，查看日志中的 `video direct send` 和 NapCat 日志。

### 视频任务很久没有结果

任务期间不发送百分比消息。检查 `video_timeout`、中转站状态和日志中的 request ID；任务结束后会释放并发槽。

### 如何只测试连接

打开 KKT Studio，点击对应通道的“测试连接”。测试只访问 health/models，不会生成图片或视频。

## 版本与兼容

- AstrBot：`>=4.17.0`
- 当前开发运行时：AstrBot `4.26.8`
- 支持平台：`aiocqhttp`、`qq_official`、`telegram`、`discord`
