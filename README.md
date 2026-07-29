# SGCC Home Assistant Bridge

[![License](https://img.shields.io/github/license/MaribelHearm/sgcc-home-assistant-bridg)](LICENSE)
[![Release](https://img.shields.io/github/v/tag/MaribelHearm/sgcc-home-assistant-bridg?label=release)](https://github.com/MaribelHearm/sgcc-home-assistant-bridg/tags)
[![CI and Docker Image](https://github.com/MaribelHearm/sgcc-home-assistant-bridg/actions/workflows/docker-image.yml/badge.svg)](https://github.com/MaribelHearm/sgcc-home-assistant-bridg/actions/workflows/docker-image.yml)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-MQTT%20Discovery-41BDF5)](https://www.home-assistant.io/integrations/mqtt/)

把国家电网 / 网上国网 / 95598 的电费余额、日用电、月度用电、年度用电和峰平谷尖分时电量接入 Home Assistant。

本项目适合已经在用 Home Assistant，希望把国网用电数据放进仪表盘、自动化、长期历史和能源看板的家庭用户。

> 非官方项目。基于 [`ARC-MX/sgcc_electricity_new`](https://github.com/ARC-MX/sgcc_electricity_new) 二次开发，保留 Apache-2.0 License、NOTICE 和上游来源说明。

## 特性

- 抓取电费余额、欠费、日/月/年用电数据。
- 支持峰 / 平 / 谷 / 尖分时电量。
- 使用 SQLite 保存本地事实库，方便重发布和排障。
- 支持 MQTT Discovery 自动生成 Home Assistant 设备和实体。
- 支持 MQTT Discovery、HA REST states API 或两者同时发布。
- 支持 Docker Compose、GHCR 镜像和 Home Assistant OS / Supervised Add-on。
- 默认提供官方 Google Chrome `browser-service` 模式，减少无人值守登录风控概率。
- 每天重新抽取可配置的抓取时间偏移，避免长期固定时刻登录。
- 支持短信验证码、二维码人工兜底，以及 Telegram Bot 通知与验证码回复。
- 验证码识别使用 OpenAI 兼容多模态接口，也兼容火山方舟 / 豆包 `ARK_*` 配置。

## 快速开始

### 1. 准备

你需要：

- 一个可登录的国家电网 / 网上国网账号；
- 一个 Home Assistant MQTT broker，推荐 Mosquitto；
- 一个支持图片输入的 OpenAI 兼容多模态接口。

### 2. 配置环境变量

```bash
cp example.env .env
$EDITOR .env
```

最小配置示例：

```env
PHONE_NUMBER="your-phone-number"
PASSWORD="your-password"

PUBLISHER="mqtt"
MQTT_HOST="127.0.0.1"
MQTT_PORT=1883
MQTT_USERNAME=""
MQTT_PASSWORD=""

LLM_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"
LLM_API_KEY="ark-xxxxxxxx"
LLM_MODEL="ep-xxxxxxxx"
```

### 3. 启动

```bash
docker compose pull
docker compose up -d
```

查看日志：

```bash
docker compose logs -f sgcc_electricity_app
```

如果需要本地构建：

```bash
docker compose build
docker compose up -d
```

Home Assistant OS / Supervised 可以添加 Add-on/App 仓库：

```text
https://github.com/MaribelHearm/sgcc-home-assistant-bridg
```

完整部署说明见 [DOCS.md](DOCS.md)。

## v0.1.5 及更早版本升级：实体 ID 兼容

v0.1.6 引入了防碰撞、隐私化的 canonical 账户身份 `末四位_稳定摘要`。如果旧 Lovelace、自动化或脚本仍引用 v0.1.5 及更早的 MQTT 实体，升级到 v0.1.8 后请先保持默认配置：

```env
MQTT_LEGACY_DISCOVERY_MODE="compat"
```

`compat` 会先发布 canonical v2 实体，再为**末四位唯一**的户号恢复原 v0.1.5 MQTT Discovery 身份；不需要 Home Assistant API，也不要求改用 `PUBLISHER=both`。旧别名复用 canonical 状态主题，不是第二份数据源。若同一账号下有多个户号末四位相同，旧身份无法证明归属，程序会安全撤销该别名，只保留各自独立的 canonical 实体。

不要在仪表盘、自动化、脚本等消费者全部迁移前启用 `cleanup`。`PUBLISHER=rest|mqtt|both` 均继续支持；HA UI/API 重命名 canonical 实体是可选的高级迁移路径，不是升级前提。完整的验证、迁移与回滚步骤见 [实体身份迁移说明](docs/entity-identity-migration.md)。

## 实体和数据

推荐使用 `PUBLISHER=mqtt`。项目会通过 MQTT Discovery 创建一组设备实体。

| 数据 | 说明 |
| --- | --- |
| 余额 / 欠费 | 电费余额、预付费余额、应交金额。 |
| 日用电 | 最近日用电和独立 `daily_YYYYMMDD` 历史实体。 |
| 月度用电 | 月度用电、月度电费和独立 `monthly_YYYYMM` 历史实体。 |
| 年度用电 | 年度用电、年度电费和独立 `year_YYYY` 历史实体。 |
| 峰平谷尖 | 当前月已抓到日读数汇总后的分时电量。 |
| 曲线数据 | `history` 实体属性里的 `daily` / `monthly` 数组。 |

HA REST states API 发布仍可用，并与 MQTT 共用防碰撞账户身份。详细实体说明见 [DOCS.md#5-home-assistant-实体](DOCS.md#5-home-assistant-实体)。

数据缺失、登录异常、发布异常或金额口径不一致时，按 [DOCS.md#debug-模式与-issue-反馈](DOCS.md#debug-模式与-issue-反馈) 开启 `SGCC_DEBUG=true`，运行一次后附上脱敏 Debug bundle。

## Debug 模式

提交 issue 前可以临时开启：

```env
SGCC_DEBUG=true
```

开启后重新运行一次抓取，程序沿用同一条生产抓取链路，并额外保存 Network、Vuex、Vue Component、DOM 语义、结构指纹、候选字段和 parser decision：

```text
/data/debug/latest/summary.txt
/data/debug/latest/browser-runtime.json
/data/debug/latest/login-network.json
/data/debug/latest/observations.redacted.json
/data/debug/latest/parser-decisions.json
/data/debug/latest/sgcc-debug-bundle.zip
```

Debug 数据递归脱敏，并对通用 `label/value` 结构中的姓名、地址、联系方式、账号和凭证值做关联脱敏；响应体、组件、节点、深度和执行时间均有硬上限。Debug 目录固定为 `0700`、文件固定为 `0600`。未知字段不会直接作为金额发布；预算内的字段和值、截断位置和结构差异会进入 bundle，后续可直接转成 fixture/adapter。旧 `SGCC_DIAG=true` 保持兼容。

生产解析与 Debug 取证已经隔离：开启 Debug 前后使用同一组生产 observation，完整 Component `$data` 和额外 DOM 只进入诊断包。金额字段统一登记在 `sgcc_ha_bridge/field_contracts.py`，新增兼容项需要 Debug 样本、fixture 和正负测试。

## Lovelace 示例

示例和截图在：

```text
examples/
assets/lovelace-cards/
```

常用入口：

- [examples/README.md](examples/README.md)：示例目录索引。
- [examples/lovelace-cards/](examples/lovelace-cards/)：三套内置卡片示例。
- [docs/entity-identity-migration.md](docs/entity-identity-migration.md)：v0.1.5 旧实体兼容、canonical 迁移、验证和回滚。
- [docs/state-grid-lovelace-migration.md](docs/state-grid-lovelace-migration.md)：`state_grid` 仪表盘字段替换说明。

已有 `state_grid` 仪表盘 YAML 时，不建议让后端兼容另一套实体模型；可以用 `tools/convert_state_grid_lovelace.py` 做离线字段替换。

## 文档

- [DOCS.md](DOCS.md)：完整配置、部署、实体、浏览器模式和排障。
- [example.env](example.env)：环境变量示例。
- [docs/development.md](docs/development.md)：仓库结构和本地开发。
- [docs/release.md](docs/release.md)：版本、镜像和发布流程。
- [examples/README.md](examples/README.md)：示例目录索引。
- [CHANGELOG.md](CHANGELOG.md)：版本记录。
- [NOTICE](NOTICE)：上游来源与版权说明。

## 开发

项目源码在 `sgcc_ha_bridge/`，`scripts/` 只保留 Docker/Add-on shell 入口脚本。

本地验证：

```bash
python -m unittest discover -s tests -v
```

更多说明见 [CONTRIBUTING.md](CONTRIBUTING.md) 和 [docs/development.md](docs/development.md)。

## 许可证

Apache-2.0。详见 [LICENSE](LICENSE)。
