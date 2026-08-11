# SGCC Home Assistant Bridge Add-on/App 安装教程

本项目是国家电网 / SGCC / 95598 电费与用电数据接入 Home Assistant 的非官方桥接 Add-on/App，基于 [`ARC-MX/sgcc_electricity_new`](https://github.com/ARC-MX/sgcc_electricity_new) Apache-2.0 二开。

仓库地址：

```text
https://github.com/MaribelHearm/sgcc-home-assistant-bridg
```

当前状态：

- 预构建镜像先支持 `amd64`。
- 当前 Add-on 版本 `v0.1.9`，默认使用官方 Google Chrome `browser-service` 模式。
- Add-on 是单容器部署，镜像内已经包含官方 `google-chrome-stable` 和匹配 ChromeDriver；用户不需要在 HAOS、宿主机或 NAS 上另装 Google Chrome。
- 已在 HAOS 18.0 / Supervisor 2026.06.2 上验证仓库添加、识别、安装和启动。
- 真实国网账号抓取、LLM 验证码和 MQTT 发布建议按自己的账号环境再跑一轮。
- 本页截图来自当前项目 `SGCC Home Assistant Bridge` 的 HAOS 测试环境，配置截图已遮挡手机号、密码、Key、Token 等敏感信息。

## 安装步骤

### 1. 添加 Add-on/App 仓库

1. 打开 Home Assistant。
2. 进入 **设置**。
3. 打开 **Add-ons / Apps / 加载项**。
4. 进入 **Add-on Store / App Store / 加载项商店**。
5. 点击右上角三个点，选择 **Repositories / 仓库**。
6. 添加本项目仓库地址：

```text
https://github.com/MaribelHearm/sgcc-home-assistant-bridg
```

7. 保存后刷新 Store。

![添加当前项目 Add-on/App 仓库](img/current/01-add-repository.png)

### 2. 安装 SGCC Home Assistant Bridge

1. 在 Store 中找到 **SGCC Home Assistant Bridge**。
2. 打开详情页。
3. 点击 **Install / 安装**。
4. 等待安装完成。

![Store 中识别到 SGCC Home Assistant Bridge](img/current/02-store-addon.png)

如果列表里没有出现：

- 确认仓库地址没有拼错，仓库名是 `sgcc-home-assistant-bridg`。
- 刷新 Store。
- 确认当前 HAOS / Supervised 主机架构是 `amd64`。

### 3. 配置

安装完成后进入 **Configuration / 配置**，填写常用项：

| 配置项 | 说明 |
| --- | --- |
| `PHONE_NUMBER` | 国家电网 / 网上国网账号手机号。 |
| `PASSWORD` | 国家电网 / 网上国网密码。 |
| `IGNORE_USER_ID` | 需要忽略的户号，多个用英文逗号分隔，可留空。 |
| `LLM_BASE_URL` | OpenAI 兼容多模态接口 Base URL。 |
| `LLM_API_KEY` | OpenAI 兼容多模态接口 Key。 |
| `LLM_MODEL` | 多模态模型名或火山方舟 `ep-...` 接入点 ID。 |
| `PUBLISHER` | 发布方式：`mqtt`、`rest` 或 `both`；推荐 `mqtt`。旧 MQTT 实体兼容不要求改为 `both`。 |
| `MQTT_HOST` | MQTT broker 地址。 |
| `MQTT_PORT` | MQTT broker 端口，通常是 `1883`。 |
| `MQTT_USERNAME` | MQTT 用户名，可留空。 |
| `MQTT_PASSWORD` | MQTT 密码，可留空。 |
| `MQTT_DISCOVERY_PREFIX` | 通常保持 `homeassistant`。 |
| `MQTT_LEGACY_DISCOVERY_MODE` | v0.1.8 旧实体迁移模式，默认 `compat`；旧消费者全部迁移前不要改成 `cleanup`。 |
| `SGCC_DEBUG` | 提交 issue 前可临时设为 `true`；写入 `/data/debug/latest/sgcc-debug-bundle.zip` 完整脱敏取证包。 |
| `SGCC_DIAG` | 旧诊断开关兼容别名，等价于 `SGCC_DEBUG`。 |
| `SGCC_BROWSER_MODE` | 默认 `browser-service`；如需回滚旧 Chromium 模式，可改为 `local`。 |
| `SGCC_BROWSER_SERVICE_STOP_ON_RELEASE` | 默认 `true`；每轮抓取结束后关闭 Chrome 本体，降低常驻资源占用。 |
| `SGCC_DAILY_JITTER_MINUTES` | 每日抓取相对基准时间的随机偏移窗口，默认 `10` 分钟；持续运行时每天重新抽取。 |
| `SGCC_LOGIN_FALLBACK_METHODS` | 人工登录兜底顺序，例如 `phone-code,qrcode`。 |
| `SGCC_LOGIN_INTERACTION_PROVIDER` | 登录交互提供方；使用 Telegram 时设为 `telegram`。 |
| `SGCC_TELEGRAM_BOT_TOKEN` | Telegram Bot Token；敏感字段，只发送到你控制的 Bot。 |
| `SGCC_TELEGRAM_CHAT_ID` | 唯一允许接收二维码并回复短信验证码的私聊 Chat ID。 |
| `SGCC_LOGIN_FALLBACK_UNATTENDED` | 定时任务是否等待人工短信/扫码；默认 `false`。 |
| `SGCC_RISK_FALLBACK_OVERRIDE` | RK001 后是否允许人工接管；默认 `false`，保持直接冷却。 |

![Configuration 页面示例，敏感字段已遮挡](img/current/04-configuration-redacted.png)

火山方舟 / 豆包示例：

```text
LLM_BASE_URL = https://ark.cn-beijing.volces.com/api/v3
LLM_API_KEY  = ark-xxxxxxxx
LLM_MODEL    = ep-xxxxxxxx
```

```text
SGCC_LOGIN_FALLBACK_METHODS = phone-code,qrcode
SGCC_LOGIN_INTERACTION_PROVIDER = telegram
SGCC_TELEGRAM_BOT_TOKEN = <your-bot-token>
SGCC_TELEGRAM_CHAT_ID = <your-private-chat-id>

# 安全默认：定时任务不等待人工响应，RK001 直接冷却
SGCC_LOGIN_FALLBACK_UNATTENDED = false
SGCC_RISK_FALLBACK_OVERRIDE = false
```

启用后，二维码会发送到配置的私聊；短信验证码必须直接回复 Bot 本次发出的提示消息，程序只接受 4～8 位纯数字。默认配置不会让 Add-on 的定时任务停下来等待人工输入。只有已经确认“密码登录持续 RK001、短信登录仍可用”时，才应谨慎开启 `SGCC_RISK_FALLBACK_OVERRIDE`；定时任务还必须同时开启 `SGCC_LOGIN_FALLBACK_UNATTENDED`。详细安全边界与排障说明见 [`DOCS.md`](../DOCS.md#telegram-登录交互)。

### 4. 启动

1. 保存配置。
2. 回到 **Info / 信息** 页面。
3. 点击 **Start / 启动**。
4. 查看 **Logs / 日志**。

![Add-on/App 详情页](img/current/03-addon-detail.png)

![Add-on/App 运行中状态，页面显示停止和重启按钮](img/current/05-addon-started.png)

启动后程序会：

- 读取 Add-on 配置。
- 启动内置 Xvfb 和轻量 browser manager。
- 默认使用官方 `google-chrome-stable`，在登录/抓取前按需启动 Chrome，通过 CDP attach，任务结束后关闭 Chrome 本体。
- 尝试从 SQLite 缓存恢复数据。
- 缓存不可用时登录国网页面抓取数据。
- 通过 MQTT Discovery / REST 发布到 Home Assistant。

### 5. 查看 Home Assistant 实体

MQTT Discovery 正常后，Home Assistant 会出现类似下面的设备：

```text
国网电费 ****1234
```

常见实体包括：

- 电费余额 / 预付费余额 / 应交金额
- 最近日用电
- 月度用电 / 月度电费
- 年度用电 / 年度电费
- 月度谷/平/峰/尖时电量
- `history` 历史数据实体
- `daily_YYYYMMDD` 日历史实体
- `monthly_YYYYMM` 月历史实体

canonical MQTT 实体默认使用 `sensor.sgcc_<末四位_稳定摘要>_<key>`，例如 `sensor.sgcc_0123_e2161a7e19_balance`。从 v0.1.5 或更早版本升级时保持 `MQTT_LEGACY_DISCOVERY_MODE=compat`，程序会自动恢复无冲突的旧 MQTT Discovery 身份；不需要 HA API，也不需要把 `PUBLISHER` 改成 `both`。同一账号有多个户号末四位相同时，旧身份无法区分归属，只保留各自 canonical 实体。消费者迁完前不要启用 `cleanup`，详见 [`v0.1.8 实体身份兼容与迁移`](../docs/entity-identity-migration.md)。

## 常见问题

### 找不到 Add-on/App

- 检查仓库地址是否为：`https://github.com/MaribelHearm/sgcc-home-assistant-bridg`
- 刷新 Store。
- 确认主机架构是 `amd64`。

### 安装失败

- 检查 HAOS/Supervisor 能否拉取 GHCR 镜像。
- 镜像为：`ghcr.io/maribelhearm/sgcc-home-assistant-bridge:v0.1.9`
- 国内网络如果拉取 GHCR 很慢，可以先确认 HAOS/Supervisor 能访问 GHCR；当前 Add-on 默认使用 GHCR app 镜像。

### 验证码/登录未通过

- 确认 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 正确。
- 确认模型支持图片输入。
- 火山方舟建议 `LLM_MODEL` 填 `ep-...` 接入点 ID。
- 默认浏览器模式应为 `SGCC_BROWSER_MODE=browser-service`；如需临时回滚旧模式，可改为 `local` 后重启 Add-on。

### MQTT 实体未出现

- 确认 Home Assistant 已配置 MQTT 集成。
- 确认 `MQTT_HOST` 是 Add-on 容器能访问到的 broker 地址。
- 确认 `MQTT_DISCOVERY_PREFIX=homeassistant`。
- 查看 Add-on 日志是否有 MQTT 连接失败。

### 抓取失败

- 查看 Add-on 日志。
- 需要反馈 issue 时，把 `SGCC_DEBUG` 临时设为 `true` 后重新运行一次，附上 `/data/debug/latest/sgcc-debug-bundle.zip`。Debug 目录权限为 `0700`，包和内部源文件权限为 `0600`。
- 查看 `/data/errors` 中的 `meta.redacted.json`。该目录默认不保存原始 HTML 和截图，并只保留最近 10 次错误现场。
- 如果出现 `RK001`，通常是 95598 登录风控命中。本项目默认停止本轮并进入冷却，避免反复打账号。短信/二维码人工接管默认不会在 RK001 后自动启用，详见 [`DOCS.md`](../DOCS.md#rk001)。
- Add-on 和 Docker Compose 默认都使用 `SGCC_BROWSER_MODE=browser-service`。Add-on 内嵌官方 Google Chrome browser manager；Docker Compose 使用官方 Google Chrome sidecar。
- 如果当前环境不适合新模式，可以把 `SGCC_BROWSER_MODE` 改成 `local` 回滚到 Debian Chromium + Xvfb。
