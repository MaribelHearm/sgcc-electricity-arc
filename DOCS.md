# SGCC Home Assistant Bridge 详细文档

本文放 README 没展开的细节：配置、实体、架构、故障排查、上游关系和限制说明。

## 1. 项目定位

SGCC Home Assistant Bridge 是国家电网 / 网上国网 / 95598 到 Home Assistant 的非官方本地桥接程序。

主链路：

```text
schedule / startup
  -> account login/session check
       password + Tencent click captcha via multimodal LLM
       qrcode fallback only when configured/needed
  -> browser mode
       Docker Compose default: official Google Chrome sidecar + CDP attach
       compatible fallback: Debian Chromium + Xvfb + ChromeDriver
  -> Path B scraper
       Vuex $store snapshot + component data
  -> parser / normalized AccountData model
  -> SQLite /data/sgcc.sqlite3 fact store
       data + fetch runs + session checks + publisher state
  -> Home Assistant publisher
       MQTT Discovery device/entities
       REST states API compatibility
```

主要模块：`config`、`redact`、`browser`、`login`、`session`、`scraper`、`parser`、`store`、`model`、`ha_mapping`、`sensor_updator`、`mqtt_publisher`、`captcha_selenium`、`click_captcha_solver`、`llm_config`。

## 2. 部署方式

### Docker Compose 本地构建

```bash
cp example.env .env
$EDITOR .env
docker compose build
docker compose up -d
```

默认 compose 会启动两个服务：

- `sgcc_browser`：使用 `Dockerfile.browser` 构建，安装官方 `google-chrome-stable`，提供 `/start`、`/stop`、`/status` 管理接口。
- `sgcc_electricity_app`：使用 `Dockerfile-for-github-action` 构建，读取 `.env`，负责登录、抓取、解析和 HA/MQTT 发布。

默认运行方式：

- `SGCC_BROWSER_MODE=browser-service`。
- `sgcc_browser` 通过 host 网络监听 `127.0.0.1:39222` 管理接口和 `127.0.0.1:19222` Chrome CDP。
- 主程序每轮登录/抓取前调用 sidecar `/start`，Selenium 通过 CDP attach；任务结束后默认调用 `/stop` 关闭 Chrome。
- Chrome profile 默认挂载到 `/data/sgcc-browser-profile`；app 数据、SQLite 和错误现场仍使用 `/data`。
- 通过 `restart: unless-stopped` 常驻调度。常驻的是主程序和轻量 sidecar 管理器，不是完整 Chrome 进程。

### GHCR 镜像

`latest` 跟随 GitHub `main` 分支发布。需要固定构建时，可以使用版本 tag 或 `sha-xxxxxxx` tag。

`docker-compose.yml` 已同时声明 app 镜像和 browser-service 镜像；普通用户可以直接拉预构建镜像：

```bash
docker compose pull
docker compose up -d
```

默认 GHCR 镜像：

```env
SGCC_APP_IMAGE=ghcr.io/maribelhearm/sgcc-home-assistant-bridge:latest
SGCC_BROWSER_IMAGE=ghcr.io/maribelhearm/sgcc-home-assistant-bridge-browser:latest
```

固定版本可以使用：

```env
SGCC_APP_IMAGE=ghcr.io/maribelhearm/sgcc-home-assistant-bridge:v0.1.9
SGCC_BROWSER_IMAGE=ghcr.io/maribelhearm/sgcc-home-assistant-bridge-browser:v0.1.9
```

Compose 使用 `browser-service` 时，app/browser 两个镜像建议固定到同一个 tag，避免 app 内 ChromeDriver 与 browser-service Chrome 版本不一致。

browser-service 的 `/status` 会返回镜像 `revision`。主程序每次启动 sidecar 时也会记录
`app_revision` 和 `browser_revision`；两者都已标记且不一致时会输出警告。排查部署版本时可先执行：

```bash
curl -s http://127.0.0.1:39222/status
docker inspect sgcc_electricity_arc --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^VERSION='
```

正式发布的 app/browser 镜像由同一次 CI 构建注入相同的 Git commit SHA。若使用自建镜像，
应给两个 Dockerfile 传入相同的 `VERSION` build arg。

也可以继续本地构建：

```bash
docker compose build
docker compose up -d
```

### 国内镜像：阿里云 ACR

国内网络访问 GHCR 慢时，Docker Compose 可以把 app 和 browser 两个镜像都换成阿里云 ACR。公开拉取不需要登录：

```env
SGCC_APP_IMAGE=crpi-uqxz2jxgnrieto82.cn-hangzhou.personal.cr.aliyuncs.com/maribelhearm/sgcc_ha:latest
SGCC_BROWSER_IMAGE=crpi-uqxz2jxgnrieto82.cn-hangzhou.personal.cr.aliyuncs.com/maribelhearm/sgcc_ha:browser-latest
```

镜像 tag 规则：

```text
# app 镜像
latest                 # 跟随 main 分支
main                   # main 分支构建
sha-xxxxxxx            # 提交短 SHA
v0.1.9                 # Git tag 发布后生成同名版本 tag

# browser-service 镜像；与 app 共用公开仓库，使用 browser-* 前缀
browser-latest
browser-main
browser-sha-xxxxxxx
browser-v0.1.9
```

例如：

```text
crpi-uqxz2jxgnrieto82.cn-hangzhou.personal.cr.aliyuncs.com/maribelhearm/sgcc_ha:main
crpi-uqxz2jxgnrieto82.cn-hangzhou.personal.cr.aliyuncs.com/maribelhearm/sgcc_ha:browser-main
crpi-uqxz2jxgnrieto82.cn-hangzhou.personal.cr.aliyuncs.com/maribelhearm/sgcc_ha:v0.1.9
crpi-uqxz2jxgnrieto82.cn-hangzhou.personal.cr.aliyuncs.com/maribelhearm/sgcc_ha:browser-v0.1.9
```

本仓库默认分支为 `main`。CI 会同时发布 GHCR app/browser 镜像；阿里云 ACR 使用同一个公开 `sgcc_ha` 仓库发布 app 普通 tag 和 browser `browser-*` tag。

Home Assistant Add-on / App 默认仍使用 GHCR app 镜像；Add-on 是单容器模式，不需要单独拉 browser 镜像。

### 浏览器模式

项目通过 `.env` 的 `SGCC_BROWSER_MODE` 切换浏览器运行方式：

| 模式 | 适用场景 | 行为 |
| --- | --- | --- |
| `browser-service` | Docker Compose / Home Assistant Add-on 默认；推荐给群晖 NAS / x86 小主机 / Linux Server | 官方 Google Chrome；Compose 使用 `sgcc_browser` sidecar，Add-on 单容器内嵌 browser manager；抓取时按需启动 Chrome，app 通过 CDP attach，用完关闭 Chrome |
| `local` | 兼容旧部署 / 回滚测试 | app 容器内 Debian Chromium + Xvfb + ChromeDriver |
| `host-cdp` / `cdp` | 高级调试或真实桌面测试 | app 连接外部已启动的 Chrome CDP 地址，不负责启动/关闭 Chrome |

推荐配置：

```env
SGCC_BROWSER_MODE=browser-service
SGCC_CDP_ADDRESS=127.0.0.1:19222
SGCC_BROWSER_SERVICE_URL=http://127.0.0.1:39222
SGCC_BROWSER_SERVICE_STOP_ON_RELEASE=true
SGCC_BROWSER_SERVICE_PROFILE_HOST=/data/sgcc-browser-profile
SGCC_BROWSER_SERVICE_PROFILE=/data/sgcc-browser-profile
```

`browser-service` 的目标是把登录环境从容器内 Debian Chromium 换成官方 Google Chrome，同时避免完整 Chrome 长期常驻。Compose 常驻主程序和轻量 sidecar；Add-on 常驻主程序、Xvfb 和轻量 browser manager。两种部署都只在抓取/登录前拉起 Chrome 本体，并在任务结束后默认关闭。Chrome 关闭后 profile 仍保留，但国网页面本身不保证浏览器关闭后登录态可复用，所以不要把它当成免登录方案。

#### 自定义 Docker network 访问 sidecar

默认 Compose 使用 `network_mode: host`，app 直接访问 `127.0.0.1:39222` 和 `127.0.0.1:19222`。少数用户如果改成自定义 Docker network，并希望通过服务名访问 browser sidecar，需要同时开放 browser manager 和可选 CDP 代理：

```env
SGCC_BROWSER_SERVICE_HOST=0.0.0.0
SGCC_BROWSER_SERVICE_URL=http://sgcc_browser:39222
SGCC_BROWSER_CDP_HOST=0.0.0.0
SGCC_BROWSER_CDP_PORT=19222
SGCC_BROWSER_CDP_INTERNAL_PORT=19223
SGCC_BROWSER_CDP_FORWARD_ENABLED=true
SGCC_CDP_ADDRESS=sgcc_browser:19222
```

`SGCC_BROWSER_CDP_FORWARD_ENABLED` 默认关闭。开启后 Chrome 本体使用 `SGCC_BROWSER_CDP_INTERNAL_PORT` 作为内部 loopback CDP 端口，browser-service 再在 `SGCC_BROWSER_CDP_HOST:SGCC_BROWSER_CDP_PORT` 上提供内置代理，兼容新版 Chrome 强制 `127.0.0.1` 监听的行为。只在私有 Docker network 中按需开启，不建议暴露到公网或不可信网络。

如需回滚旧模式：

```env
SGCC_BROWSER_MODE=local
```

### Home Assistant Add-on / App

Home Assistant OS / Supervised 用户可以直接把本仓库作为 Add-on/App 仓库添加：

```text
https://github.com/MaribelHearm/sgcc-home-assistant-bridg
```

安装入口：设置 → Add-ons/Apps → Add-on Store → 右上角 Repositories → 添加上面的仓库地址 → 刷新。

说明：

- 当前预构建镜像只发布 `amd64`，所以 `config.yaml` 也先只声明 `amd64`。
- `config.yaml` 的 `version` 使用 `v0.1.9`，与 GHCR tag 对齐。
- Add-on/App 使用 GHCR app 镜像：`ghcr.io/maribelhearm/sgcc-home-assistant-bridge:v0.1.9`。
- Add-on/App 是单容器部署，镜像内已经安装官方 `google-chrome-stable` 和匹配 ChromeDriver；用户不需要在 HAOS、宿主机或 NAS 上另装 Google Chrome。
- Add-on/App 默认 `SGCC_BROWSER_MODE=browser-service`，入口脚本会启动内嵌 browser manager；Chrome 本体只在抓取/登录时按需启动，任务结束后默认关闭。
- 已在 HAOS 18.0 / Supervisor 2026.06.2 上验证仓库添加、识别、安装和启动；真实国网登录、LLM 验证码和 MQTT 发布仍建议按自己的账号环境跑一轮。
- 安装完成后进入 “配置 / Configuration”。
- 填写国家电网账号密码、MQTT、LLM 验证码接口；只有使用 `rest`/`both` 时才需要 REST 相关配置。
- 推荐使用 `PUBLISHER=mqtt`；需要同时发布 HA REST state 时使用 `PUBLISHER=both`。
- 保存后在 “信息 / Info” 中启动。
- 失败时查看日志和 `/data/errors`。

带截图的安装教程见：[`ha_addons_doc/Add-on教程.md`](ha_addons_doc/Add-on教程.md)。

## 3. 配置项

| 变量 | 用途 |
| --- | --- |
| `PHONE_NUMBER` | 国家电网登录手机号/账号。 |
| `PASSWORD` | 国家电网登录密码。 |
| `IGNORE_USER_ID` | 忽略指定户号，多个用英文逗号分隔。 |
| `HASS_URL` | Home Assistant 地址，REST 发布使用。 |
| `HASS_TOKEN` | Home Assistant 长期访问令牌，REST 发布使用。 |
| `JOB_START_TIME` | 每日抓取基准时间，格式 `HH:MM`。 |
| `SGCC_DAILY_JITTER_MINUTES` | 每日抓取相对基准时间的随机偏移窗口，范围 `0..180`，默认 `10`（即 ±10 分钟）；设为 `0` 关闭。持续运行期间每天重新抽取。 |
| `SGCC_DAILY_RUNS` | 每天真实登录抓取次数；无人值守建议保持 `1`，设为 `2` 可恢复早晚两次；当天两次共用同一偏移并保持 12 小时间隔。 |
| `RETRY_TIMES_LIMIT` | 登录、验证码或抓取失败时的重试次数上限；风控类失败会熔断，不参与立即重试。 |
| `RISK_COOLDOWN_MINUTES` | RK001/操作频繁/验证码通过但仍失败后的登录冷却分钟数，默认 `60`。 |
| `SGCC_LOGIN_COOLDOWN_ENABLED` | 是否启用无人值守登录冷却，建议保持 `true`。 |
| `SGCC_RISK_FALLBACK_OVERRIDE` | RK001 后是否允许尝试已配置的短信验证码/二维码人工接管；默认 `false`，保持直接冷却。它不绕过任务级 fallback 门控，也不是风控重试开关。 |
| `SGCC_LOGIN_FALLBACK_UNATTENDED` | 定时无人值守任务是否允许短信验证码/二维码交互兜底；默认 `false`，避免无人响应时挂起。手动任务始终允许已配置的兜底方式。 |
| `SGCC_QRCODE_FALLBACK_UNATTENDED` | 旧二维码专用兼容开关；建议新配置使用 `SGCC_LOGIN_FALLBACK_UNATTENDED`。 |
| `SGCC_LOGIN_FALLBACK_METHODS` | 密码/点选验证码失败后的兜底顺序，逗号分隔；支持 `phone-code`、`qrcode`，例如 `phone-code,qrcode`。未设置时兼容读取 `LOGIN_FALLBACK`。 |
| `SGCC_LOGIN_INTERACTION_PROVIDER` | 登录人工交互提供方：`auto`、`telegram`、`url`、`both`、`none`；默认 `auto`，启用已完整配置的 Telegram，并兼容 `PUSH_QRCODE_URL`。 |
| `SGCC_TELEGRAM_BOT_TOKEN` | Telegram Bot Token；敏感配置，不进入日志和 Debug 环境快照。兼容 `TG_BOT_TOKEN`。 |
| `SGCC_TELEGRAM_CHAT_ID` | 唯一允许接收二维码和回复短信验证码的 Telegram Chat ID。兼容 `TG_CHAT_ID`。 |
| `SGCC_TELEGRAM_API_BASE_URL` | Telegram Bot API 地址，默认 `https://api.telegram.org`；兼容 `TG_API_BASE_URL`。仅接受无 URL 凭证、查询参数或 fragment 的 HTTPS 地址。 |
| `SGCC_SMS_CODE_TIMEOUT_SECONDS` | 等待短信验证码回复的秒数，范围 `30..600`，默认 `180`。 |
| `LLM_API_KEY` | OpenAI 兼容多模态接口 Key；也兼容 `ARK_API_KEY`。 |
| `LLM_BASE_URL` | OpenAI 兼容接口 Base URL；也兼容 `ARK_BASE_URL`。 |
| `LLM_MODEL` | 多模态模型名或接入点 ID；也兼容 `ARK_MODEL`。 |
| `LOGIN_FALLBACK` | 旧登录兜底配置；`qrcode` 表示二维码人工扫码。新配置建议使用 `SGCC_LOGIN_FALLBACK_METHODS`。 |
| `PUBLISHER` | 发布方式：`mqtt`、`rest`、`both`；推荐 `mqtt`。`both` 会同时发布 MQTT Discovery 和 HA REST state。 |
| `MQTT_HOST` | MQTT broker 地址。 |
| `MQTT_PORT` | MQTT broker 端口。 |
| `MQTT_USERNAME` | MQTT 用户名，可留空。 |
| `MQTT_PASSWORD` | MQTT 密码，可留空。 |
| `MQTT_DISCOVERY_PREFIX` | Home Assistant MQTT Discovery 前缀，默认 `homeassistant`。 |
| `MQTT_LEGACY_DISCOVERY_MODE` | MQTT 旧实体迁移模式：`compat`（默认）为末四位唯一的账户恢复 v0.1.5 Discovery 别名；`off` 不创建也不删除旧 Discovery；`cleanup` 在 canonical v2 成功后删除旧 Discovery。所有旧消费者迁移完成前不要使用 `cleanup`。 |
| `SGCC_DB_PATH` | SQLite 数据库路径，默认 `/data/sgcc.sqlite3`。 |
| `SCRAPER_SETTLE_SECONDS` | Path B 抓取等待 Vuex/组件数据稳定的秒数。 |
| `SGCC_DEBUG` | 完整 Debug 取证模式，默认 `false`；生产解析始终使用相同的轻量 observation，完整 Component/DOM 仅进入诊断取证，因此开启前后抓取和发布结果保持一致。 |
| `SGCC_DEBUG_DIR` | Debug bundle 目录，默认 `/data/debug`；最新一次固定在 `/data/debug/latest`。 |
| `SGCC_ERROR_SCREENSHOT` | 错误现场截图开关，默认 `false`；`/data/errors` 默认只保存脱敏 metadata。截图无法自动可靠清除页面可见姓名/地址，开启后需人工检查。 |
| `SGCC_ERROR_MAX_CAPTURES` | `/data/errors` 最多保留的错误现场目录数，默认 `10`。 |
| `SGCC_DIAG` | 旧诊断开关兼容别名；设为 `true` 等价于 `SGCC_DEBUG=true`。 |
| `SGCC_DIAG_DIR` | 旧诊断目录兼容配置；未设置 `SGCC_DEBUG_DIR` 时使用。 |
| `DEBUG_MODE` | 旧环境变量兼容别名；仅开启 Debug，不再切换手机验证码登录。 |
| `SGCC_CLEANUP_LEGACY_ENTITY_IDS` | 设为 `true` 时清理旧版仅使用户号末四位的 REST entity；默认 `false`，用于完成 REST 消费者迁移后的一次性清理。它不影响 MQTT；MQTT 使用 `MQTT_LEGACY_DISCOVERY_MODE=cleanup`。 |
| `PUSH_TIMEOUT` | PushPlus、URL 通知和二维码通知的 HTTP 超时秒数，默认 `10`；通知失败不会改变国网抓取或 HA/MQTT 发布状态。 |
| `SGCC_LOGIN_METHOD` | 主登录方式；默认 `password`。显式设为 `phone-code` 时直接执行短信验证码登录，验证码可从 Telegram 当前提示消息的回复中获取；交互式终端仍可本地输入。 |
| `REPUBLISH_INTERVAL_MINUTES` | 已有数据重发布或补抓的间隔分钟数。 |
| `SGCC_BROWSER_MODE` | 浏览器模式，Docker Compose 和 Add-on 默认 `browser-service`；旧模式可设 `local`。 |
| `SGCC_CDP_ADDRESS` | `browser-service` / `host-cdp` 使用的 Chrome DevTools 地址，默认 `127.0.0.1:19222`。 |
| `SGCC_BROWSER_SERVICE_URL` | browser manager 管理 API，Compose 指向 sidecar，Add-on 指向同容器内嵌服务，默认 `http://127.0.0.1:39222`。 |
| `SGCC_BROWSER_SERVICE_STOP_ON_RELEASE` | `browser-service` 每轮任务结束后是否关闭 Chrome，默认 `true`。 |
| `SGCC_BROWSER_CDP_FORWARD_ENABLED` | 可选高级开关，默认 `false`；自定义 Docker network 下需要通过服务名访问 CDP 时才开启内置代理。 |
| `SGCC_BROWSER_CDP_INTERNAL_PORT` | 内置 CDP 代理启用时 Chrome 本体使用的内部 loopback 端口，需与 `SGCC_BROWSER_CDP_PORT` 不同，默认建议 `19223`。 |
| `SGCC_BROWSER_SERVICE_PROFILE_HOST` | Compose sidecar Chrome profile 的宿主机挂载路径，默认 `/data/sgcc-browser-profile`；Add-on 不使用这个宿主机挂载变量。 |
| `SGCC_BROWSER_SERVICE_PROFILE` | browser-service 容器内/ Add-on 内 Chrome profile 路径，默认 `/data/sgcc-browser-profile`。 |
| `SGCC_BROWSER_PROFILE` | `local` 模式 Chromium 用户数据目录，默认建议放在 `/data/chrome-profile`。 |

`example.env` 还包含少量抓取等待参数，可在网络慢、页面加载不稳定或硬件性能较弱时微调。

## 4. LLM 验证码接口

项目使用 OpenAI 兼容 Chat Completions 调用多模态模型识别腾讯点选/滑块验证码。

推荐主配置：

```env
LLM_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"
LLM_API_KEY="ark-xxxxxxxx"
LLM_MODEL="ep-xxxxxxxx"
```

兼容上游 / 火山方舟写法：

```env
ARK_API_KEY="ark-xxxxxxxx"
ARK_MODEL="ep-xxxxxxxx"
```

优先级：

1. `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`
2. `ARK_API_KEY` / `ARK_BASE_URL` / `ARK_MODEL`
3. `VOLCENGINE_ARK_API_KEY` / `VOLCENGINE_ARK_BASE_URL` / `VOLCENGINE_ARK_MODEL`
4. 默认 `LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3`

火山方舟用户建议在控制台创建多模态模型接入点，`LLM_MODEL` 填 `ep-...` 接入点 ID，而不是只填模型族名称。

## 4.1 无人值守登录与风控策略

推荐无人值守配置：

```env
SGCC_DAILY_RUNS=1
SGCC_DAILY_JITTER_MINUTES=10
SGCC_LOGIN_COOLDOWN_ENABLED=true
RISK_COOLDOWN_MINUTES=60
SGCC_LOGIN_FALLBACK_UNATTENDED=false
```

说明：

- 因已支持近 30 天日用电抓取，每天一次成功抓取通常足够补齐近期历史。
- 默认不安排 12 小时后的第二次真实登录，降低晚间固定时间命中风控的概率。
- `SGCC_DAILY_JITTER_MINUTES` 默认在基准时间前后 10 分钟内偏移，设为 `0` 可关闭，最大支持 `180`。每日抽取结果会持久化到 `/data/daily_schedule.json`；持续运行期间会在当天最后一次任务结束后为下一天重新抽取，容器重启后会复用尚未执行完的逻辑日偏移。如果正偏移跨过午夜，午夜后重启仍会恢复上一逻辑日尚未到达的任务；首次启用且处于可能的跨日窗口时，也会先为上一逻辑日抽取一次并只保留仍在未来的任务。如果设置每天两次，两次共用当天偏移并保持 12 小时间隔。该配置只减少固定时刻访问特征，不保证规避或解决 RK001，也不能替代低频登录和风控冷却。
- `RETRY_TIMES_LIMIT` 仍用于登录失败或 session 明确过期等普通临时失败。登录已成功但 Path B 动态业务数据暂时为空时，程序会先在同一个浏览器 session 内恢复一次；只有明确确认 session 已过期才会重新登录。session 仍有效或状态无法确认时，恢复耗尽后会停止本轮，避免额外消耗登录次数。
- RK001、操作频繁、验证码通过但登录仍失败、验证码多次识别失败会被视为不适合立即重试。
- 短信验证码和二维码兜底都属于限时人工接管路径。默认定时任务不进入兜底；手动任务会按 `SGCC_LOGIN_FALLBACK_METHODS` 的顺序尝试。短信验证码等待超时会结束本轮外层登录重试并进入冷却，不会立即重新请求验证码；下一种已配置的本轮 fallback 方式仍可按顺序执行。
- 遇到 RK001 时默认直接进入风控冷却，不继续切换短信验证码或二维码。只有显式设置 `SGCC_RISK_FALLBACK_OVERRIDE=true`，并且本次任务原本就允许 fallback 时，才会按已配置顺序尝试一次限时人工接管；定时任务还必须同时设置 `SGCC_LOGIN_FALLBACK_UNATTENDED=true`。接管未成功仍保留原始 `risk_blocked` 分类并进入冷却。该开关适合已经确认“密码登录持续 RK001、短信登录仍可用”的场景，不是风控重试或绕过开关，也不代表 RK001 根因已解决。

### Telegram 登录交互

配置示例：

```env
SGCC_LOGIN_FALLBACK_METHODS="phone-code,qrcode"
SGCC_LOGIN_INTERACTION_PROVIDER=telegram
SGCC_TELEGRAM_BOT_TOKEN="123456:your-bot-token"
SGCC_TELEGRAM_CHAT_ID="123456789"
SGCC_SMS_CODE_TIMEOUT_SECONDS=180

# 安全默认：RK001 直接冷却；确认短信登录可用后才谨慎开启
SGCC_RISK_FALLBACK_OVERRIDE=false
# 安全默认：定时任务不等待人工响应；确认 Telegram 可用后再显式开启
SGCC_LOGIN_FALLBACK_UNATTENDED=false
```

当前行为：

- 二维码会作为 Telegram 图片发送到配置的唯一 Chat ID，扫码成功、失败或超时后发送结果通知；本地临时二维码使用 `0600` 权限并在流程结束后删除。
- 短信登录会先在 95598 页面触发“发送验证码”，然后通过 Telegram Bot 发出强制回复提示。程序只接受配置 Chat ID 对**本次提示消息**的 4～8 位纯数字回复，旧消息、其他聊天和非纯数字文本不会被当作验证码。
- 验证码不会写入日志、Debug bundle 或通知结果；超时后本轮登录安全取消。
- Telegram 使用长轮询 `getUpdates`。同一个 Bot 不应同时由其他 webhook/长轮询程序消费更新；如已有专用 Bot，建议为本项目新建独立 Bot。
- Bot API 是外部服务，二维码和短信验证码属于敏感登录材料。只向你控制的私聊 Chat ID 发送；自定义 `SGCC_TELEGRAM_API_BASE_URL` 仅接受无 URL 凭证、查询参数或 fragment 的 HTTPS 地址，并且必须信任该代理。

## 5. Home Assistant 实体

当 `PUBLISHER=mqtt` 或 `PUBLISHER=both` 且 MQTT broker 可用时，程序会向 `MQTT_DISCOVERY_PREFIX` 发布 discovery 配置。Home Assistant 会自动出现一个“国网电费 ****后四位”的 device。

推荐使用 `PUBLISHER=mqtt`，此时只生成 MQTT Discovery 设备实体。`PUBLISHER=both` 会同时发布 MQTT Discovery 和 REST state；`PUBLISHER=rest` 仍可只发布 REST。三种方式均继续支持，MQTT 旧实体兼容不要求使用 REST 或 HA API。

户号在实体名称和日志中只显示末四位。canonical MQTT topic、`unique_id`、`object_id`、`default_entity_id` 和 REST entity 后缀使用 `末四位_稳定摘要`，例如 `0123_e2161a7e19`；两个户号即使末四位相同，也会生成不同身份。完整户号不会进入发布 payload。canonical MQTT Discovery 请求的默认实体 ID 是 `sensor.sgcc_<entity-key>_<key>`，例如 `sensor.sgcc_0123_e2161a7e19_balance`。`unique_id` 决定 Home Assistant registry 中的集成身份；`default_entity_id` 是建议命名；已有 registry 记录、用户重命名或名称冲突仍可能让实际 `entity_id` 不同，因此最终以 HA 实体注册表为准，不要把 `_2` 固化为上游契约。

### v0.1.5 及更早版本升级

v0.1.6 引入 canonical v2 身份后曾自动删除旧 MQTT retained Discovery，仍引用旧实体的仪表盘和自动化可能失效。v0.1.8 默认使用 `MQTT_LEGACY_DISCOVERY_MODE=compat`：先发布 canonical v2，再在完整、权威的账户集合证明末四位唯一时恢复原 v0.1.5 `unique_id`/Discovery 身份。旧别名复用 canonical state/attributes topic，不是第二份状态源；该自愈路径不需要 Home Assistant API。

- `compat`（默认）：恢复无冲突旧别名；同尾号冲突时撤销歧义别名，只保留独立 canonical 实体。
- `off`：只发布 canonical v2，既不创建也不删除旧 retained Discovery。
- `cleanup`：canonical v2 成功后 tombstone 旧 MQTT Discovery。仅在 Lovelace、自动化、脚本和其他消费者全部迁移后使用。

被 `IGNORE_USER_ID` 忽略的账户仍参与末四位冲突判断，但不会成为旧别名 owner。非权威/不完整枚举不会新建或删除旧别名，避免部分抓取误伤。账户失效或被忽略时，程序仍清理 canonical Discovery，并仅在策略证明安全时处理旧 namespace。

旧 REST 状态兜底会从当天缓存读取完整 13 位户号，再生成新的稳定身份。旧实体仍只能按末四位读取；当缓存中多个户号末四位相同时，这批旧状态没有可证明的账户归属，程序会跳过兜底并执行一次真实国网抓取。REST 旧实体清理由独立的 `SGCC_CLEANUP_LEGACY_ENTITY_IDS` 控制，默认关闭，不与 MQTT 模式联动。

普通升级保持 `compat` 即可；通过 HA UI/API 把 canonical 实体重命名为整洁 ID 是可选高级路径。完整迁移顺序、验证和回滚见 [v0.1.8 实体身份兼容与迁移](docs/entity-identity-migration.md)。

| 类型 | Discovery key | 显示名称示例 | 说明 |
| --- | --- | --- | --- |
| 金额汇总 | `balance` / `prepay_balance` / `arrears` | 电费余额 / 预付费余额 / 应交金额 | 当前账户金额状态。 |
| 最新日数据 | `last_daily_usage` | 最近日用电 | state 是最近一天用电量；属性含 `date`、`valley_kwh`、`flat_kwh`、`peak_kwh`、`tip_kwh`。程序会尽量让国网页面返回近 30 天日用电。 |
| 最新月数据 | `month_usage` / `month_charge` | 月度用电 / 月度电费 | state 是最新月度用电或电费；属性含月份和起止日期。 |
| 本月分时汇总 | `month_valley` / `month_flat` / `month_peak` / `month_tip` | 月度谷/平/峰/尖时电量 | 由当前月已抓到的日读数汇总。 |
| 年度汇总 | `year_usage` / `year_charge` | 年度用电 / 年度电费 | state 是年度累计用电或电费。 |
| 历史摘要 | `history` | 历史数据 | state 类似 `2026-06-17 d7 m5`；属性包含 `daily`、`monthly`、年度摘要和数据范围。 |
| 日历史实体 | `daily_YYYYMMDD` | 日用电 2026-06-17 | 每个日读数一条独立实体；属性含峰平谷尖拆分。 |
| 月历史实体 | `monthly_YYYYMM` | 月度历史 2026-05 | 每个月度读数一条独立实体；属性含电费和起止日期。 |
| 年历史实体 | `year_YYYY` | 年度历史 2026 | 年度历史独立实体；属性含年度电费。 |

建议：

- 概览卡片使用 `balance`、`arrears`、`last_daily_usage`、`month_usage`、`month_charge`、`year_usage`。
- 表格或自动化使用 `daily_YYYYMMDD`、`monthly_YYYYMM`、`year_YYYY`。
- 曲线图读取 `history` 实体属性中的 `daily` / `monthly` 数组，这样横轴按国网页面日期绘制，而不是按 HA 状态更新时间绘制。

## 6. Lovelace 示例

示例文件：

```text
examples/lovelace-cards/sgcc-electricity-card-xiaoshi-original.yaml
examples/lovelace-cards/sgcc-electricity-card-xiaoshi-style.yaml
examples/lovelace-cards/sgcc-electricity-card.yaml
examples/basic/lovelace-sgcc-electricity.yaml
```

使用方式：

1. Home Assistant → 仪表盘 → 编辑仪表盘 → 添加视图/原始配置。
2. 粘贴示例内容作为一个 view。
3. 在 Home Assistant 开发者工具中确认本次生成的实际实体 ID，再替换示例实体。canonical 目标格式为 `sensor.sgcc_<末四位_稳定摘要>_<key>`；已有 registry 记录可能保留其他名称，请以 HA 实际显示为准。
4. 日/月历史实体按 HA 实际出现的数据范围增删。

`sgcc-electricity-card-xiaoshi-original.yaml`、`sgcc-electricity-card-xiaoshi-style.yaml` 和 `sgcc-electricity-card.yaml` 都已经替换成本项目实体字段，截图放在 `assets/lovelace-cards/`。其中 `sgcc-electricity-card.yaml` 来自当前自用页面 `/sgcc-electricity/overview`，依赖 `stack-in-card`、`mushroom`、`apexcharts-card` 和 `card-mod`。

曲线部分依赖 HACS 的 `apexcharts-card`；如果未安装，可以删除“历史曲线（可选 ApexCharts）”部分。

## 7. 数据与隐私

- 户号在日志、MQTT discovery、entity unique id 等位置会脱敏。
- SQLite 数据库默认保存在本机 `/data/sgcc.sqlite3`。
- 程序只会把电费/用电数据发布到你配置的 Home Assistant 和 MQTT broker。腾讯点选验证码会发送给你配置的 LLM；只有显式配置登录交互时，二维码或短信验证码提示才会发送给对应通知服务。
- 腾讯点选验证码识别会把验证码截图发送给你配置的 OpenAI 兼容 LLM 服务；请根据所选服务隐私条款自行评估。
- 请不要提交真实 `.env`、国网密码、Home Assistant Token、MQTT 凭据和 LLM API Key。


## 8. 开发结构

仓库结构、本地测试、发布流程和贡献流程见：

- [docs/development.md](docs/development.md)
- [docs/release.md](docs/release.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)

## 9. 常见问题与排障

### RK001

`网络连接超时（RK001）` 通常不像普通网络超时，更像登录页或国网服务端的风控分类。可能原因包括：

- 当前账号、IP、登录频率或会话组合被风控。
- Docker / Linux / Xvfb / CDP / browser profile 的整体环境指纹与桌面浏览器不同。
- 容器和桌面使用不同网络出口、DNS 或 IPv4/IPv6 路径。
- browser 镜像构建较早，运行中的 Google Chrome 版本与当前桌面浏览器存在差异。

Issue #23 的 2026-07-17 附件显示，Docker 流程已经完成账号密码和腾讯点选验证码，日志明确出现“验证码已通过”，随后才收到 RK001。因此该样本不是验证码识别失败，而是登录提交后的服务端拒绝。两个 app/browser 日志附件内容完全相同，现有 Debug bundle 又没有 Chrome/ChromeDriver 版本、User-Agent、`navigator.webdriver`、platform、languages、timezone、screen 等信息，所以目前**不能证明 Chrome 版本固定或过旧就是根因**。

`Dockerfile.browser` 安装的是未锁定具体版本的 `google-chrome-stable`；但镜像构建后，容器中的 Chrome 会保持在构建时版本，直到重新拉取镜像并 recreate 容器。排查时先记录：

```bash
docker exec sgcc_browser google-chrome --version
curl http://sgcc_browser:39222/json/version
```

再与成功登录的桌面浏览器对比 User-Agent、`navigator.webdriver`、platform、languages、hardware concurrency、screen、timezone 和网络出口。如果 pull/recreate browser 镜像后仍然 RK001，应优先继续比较 CDP、Xvfb、WebDriver、profile 和网络环境差异，而不是只归因于 Chrome 主版本号。

Docker Compose 和 Add-on 部署优先使用默认的 `SGCC_BROWSER_MODE=browser-service`：它把浏览器换成官方 Google Chrome，并按需启动/关闭 Chrome。Compose 通过 `sgcc_browser` sidecar 实现，Add-on 通过单容器内嵌 browser manager 实现。

项目检测到 RK001、操作过于频繁、验证码通过后仍停留登录页等情况后，默认会停止本轮立即重试，并写入 `/data/sgcc_login_cooldown.json` 冷却状态。冷却期间定时任务不会继续触发真实国网登录；已有 HA/MQTT 数据仍会优先通过缓存重发布维持展示。每日随机偏移与人工短信/二维码 fallback 只能降低固定访问特征或提供恢复通道，不能视为 RK001 根因修复。

### 验证码一直被识别为 slider

95598 页面可能预加载隐藏验证码 DOM，其中有“拖动下方拼图完成验证”等文本，但节点实际不可见。旧逻辑容易把这些隐藏 DOM 当成真实滑块验证码。

本项目会优先判断真实可见弹窗，减少隐藏 DOM 误判。

### HA 没出现实体

检查：

- MQTT broker 是否可连接。
- Home Assistant MQTT 集成是否启用 discovery。
- `MQTT_DISCOVERY_PREFIX` 是否为 `homeassistant`。
- `PUBLISHER` 是否为 `mqtt` 或 `both`。
- 容器日志里是否有 MQTT 发布失败。

### Debug 模式与 issue 反馈

`SGCC_DEBUG=true` 是面向未知省份、未知字段和抓取失败的完整取证模式。它执行和正常模式相同的账户切换、动态抓取、Store 与发布流程，仅额外保留递归脱敏证据。开启后重新运行一次抓取，程序会在日志中输出可复制的兼容摘要块：

```text
========== SGCC DIAG SUMMARY START ==========
...
========== SGCC DIAG SUMMARY END ==========
```

Debug bundle 默认写入：

```text
/data/debug/latest/
├── summary.txt
├── summary.json
├── browser-runtime.json
├── login-network.json
├── fields.redacted.json
├── observations.redacted.json
├── candidates.redacted.json
├── parser-decisions.json
├── shapes.json
├── timeline.json
└── sgcc-debug-bundle.zip
```

`browser-runtime.json` 记录登录关键阶段的 Chrome/ChromeDriver 版本、User-Agent、`navigator.webdriver`、platform、languages、timezone、screen/window、device pixel ratio、WebGL/GPU 和自动化全局变量等无凭证运行态；不读取 cookie、localStorage、sessionStorage 的值。

`login-network.json` 从 CDP Network 事件记录登录页和登录接口真正发出的白名单请求头，包括 `User-Agent`、`Accept-Language`、`sec-ch-ua`、`sec-ch-ua-platform`、`sec-ch-ua-mobile`，并与 JS 运行态生成一致性检查；同时记录 HTTP 协议、远端地址、连接复用、缓存、TLS 协议/套件及安全时序。登录阶段不读取请求体或响应 body，`Cookie`、`Authorization` 和其他未列入白名单的请求头不会写入取证包。

其中生产 observation 按户号和页面 scope 关联 Network XHR/fetch、Vuex 和受字段契约限制的 Vue Component 数据；必要的严格 DOM label/value 作为生产 fallback。完整受预算约束的 Vue Component `$data` 与额外 DOM 仅写入诊断取证，不进入 parser。Component 快照具有组件级和全局节点预算、深度/数组/字段上限及执行时间上限；截断位置保留在 bundle。parser decision 记录每个来源是接受、拒绝还是 fallback；未知金额只进入候选，不会被猜测发布。

`dev/**` 分支发布的 app 测试镜像构建时设置 `SGCC_FORCE_DEBUG=true`，即使用户配置中的 `SGCC_DEBUG=false` 也会生成上述 bundle；`main`、`latest` 和版本 tag 不强制开启。

金额字段由 `sgcc_ha_bridge/field_contracts.py` 统一登记。新增省份字段需要脱敏 Debug 样本、fixture、字段语义和正负测试；Vue capture 与 parser 共用该注册表，避免分别追加猜测 alias。

提交 issue 时优先附上 `sgcc-debug-bundle.zip` 和人工对照。包内完整户号、手机号、姓名、地址、password、token、cookie、authorization 等字段会递归脱敏；常见 `label/value`、`title/text` 和 `fieldName/fieldValue` 结构也按标签语义脱敏。Debug 根目录和每次运行目录权限为 `0700`，文件与压缩包权限为 `0600`。旧 `SGCC_DIAG=true` 保持兼容；仅开启该旧开关时继续使用 `SGCC_DIAG_DIR`。


### 日/月历史数量不固定

正常。不同地区、账号、页面状态返回的数据范围可能不同。项目只发布国网页面实际返回的数据，不额外猜测或补造历史。

### GHCR 镜像拉不下来

确认 package visibility 是 Public，然后测试：

```bash
docker pull ghcr.io/maribelhearm/sgcc-home-assistant-bridge:latest
```

国内网络也可以直接拉阿里云 ACR 镜像：

```bash
docker pull crpi-uqxz2jxgnrieto82.cn-hangzhou.personal.cr.aliyuncs.com/maribelhearm/sgcc_ha:latest
```

如果想确认镜像元数据是否可访问：

```bash
docker manifest inspect crpi-uqxz2jxgnrieto82.cn-hangzhou.personal.cr.aliyuncs.com/maribelhearm/sgcc_ha:latest
```

## 10. 和上游项目的关系

上游项目已经具备账号密码登录、LLM 视觉验证码、二维码兜底、Vue state 辅助取数、余额、日/月/年用电、峰平谷尖、REST 传感器和可选 SQLite/MySQL 等能力。

本项目主要围绕真实 Home Assistant 长期运行场景做重构：

| 方向 | 上游项目 | 本项目 |
| --- | --- | --- |
| 登录路径 | 已有账号密码登录、LLM 视觉验证码和二维码兜底 | 拆出 `login.py` / `browser.py`，增加登录态判定、受控导航、错误现场保存和脱敏日志；增加可配置的短信验证码/二维码人工接管与 Telegram 交互。 |
| 浏览器运行 | Selenium + Chrome/Chromium | Docker Compose 默认官方 Google Chrome sidecar + CDP attach，Add-on 默认单容器内嵌官方 Google Chrome browser-service，兼容旧的 Debian Chromium + Xvfb + ChromeDriver；页面/脚本超时控制，每轮用完释放。 |
| 抓取方式 | 页面取值与 Vue component data / Vue state 辅助解析 | 将 Vue/Vuex 取数主路径化，读取 store snapshot + 组件 data，交给 `parser.py` 合并归一。 |
| 数据覆盖 | 已覆盖余额、预付费/欠费、日/月/年用电、月峰/平/谷/尖等 | 统一归一、去重、落库和重发布这些业务数据，提升恢复与排障体验。 |
| 存储模型 | 可选 SQLite/MySQL，表结构偏每日用电与 key-value 扩展 | 规范化 SQLite fact store：账户、余额、日/月/年、抓取 run、会话检查、发布状态。 |
| HA 发布 | REST states API 传感器为主 | MQTT Discovery 自动建实体，同时保留 REST 兼容路径。 |
| 缓存恢复 | 有旧缓存/数据库能力 | 增加 SQLite/旧 REST 状态重发布、空缓存判定、retained MQTT state。 |

Telegram 短信/二维码交互的需求与用户体验思路参考了 Apache-2.0 项目 [`renxiaoyaoo/ha-95598`](https://github.com/renxiaoyaoo/ha-95598)；本项目基于自身登录、通知和冷却架构独立实现，未复制其代码。

## 11. 关键词

SGCC、State Grid、国家电网、网上国网、95598、Home Assistant、MQTT Discovery、SQLite、Selenium、Google Chrome、Chromium、CDP、captcha、LLM、electricity、energy。
