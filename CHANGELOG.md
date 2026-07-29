# Changelog

## [Unreleased]

### Fixed

- 腾讯验证码只处理当前 viewport 内实际显示的组件，避免把页面预加载在屏幕外的隐藏 scaffold 误判为正在进行的点选或滑块验证。
- 点选验证码主图和参考图优先从渲染后的元素截图提取，兼容当前 95598 前端使用 `currentSrc`、CSS background 或运行时资源的方式。
- 二维码登录会等待图片真正加载，并在失效时点击实际绑定刷新事件的遮罩；刷新后拒绝继续使用旧二维码。

### Diagnostics

- app/browser 镜像由同一次 CI 注入相同 Git revision；browser-service `/status` 暴露 revision，主程序启动 sidecar 时记录并检查两者是否一致。
- 登录调试包增加不含凭证的浏览器运行态、请求元数据和关键阶段时间线。

### Limitations

- 上述修改修复项目侧验证码与二维码兜底链路，不代表国网服务端 `RK001` 风控已被绕过或根治；真实账号仍需低频、受控验证。

## [v0.1.8] - 2026-07-21

### Fixed

- 修复 v0.1.5 及更早用户升级后旧 MQTT 实体被删除、Lovelace/自动化引用失效的问题：默认 `MQTT_LEGACY_DISCOVERY_MODE=compat` 会在 canonical v2 成功发布后，为末四位唯一的账户恢复原 v0.1.5 Discovery 身份。
- 同一账号存在多个末四位相同的户号时，自动撤销无法证明归属的旧别名，只保留各自防碰撞的 canonical v2 实体；非权威账户枚举不创建也不删除旧别名。
- REST 旧实体清理仅在全部 delete 成功或目标不存在后标记完成，部分失败会在后续发布重试。

### Added

- canonical MQTT Discovery 显式发布 `default_entity_id=sensor.sgcc_<末四位_稳定摘要>_<key>`，冻结新安装的实体命名契约。
- 新增 `MQTT_LEGACY_DISCOVERY_MODE=compat|off|cleanup`；`off` 不触碰旧 retained Discovery，`cleanup` 仅供所有旧消费者迁移完成后显式清理。
- 新增独立实体迁移文档、canonical Lovelace 示例契约和 v0.1.5 → v0.1.8 回归测试。

### Compatibility

- `PUBLISHER=rest|mqtt|both` 全部保留；默认升级无需 Home Assistant API，也不要求改用 `both`。
- v0.1.6 引入 `末四位_稳定摘要` 身份时会删除旧 MQTT Discovery，属于需要消费者迁移的实体身份变更；v0.1.8 为该升级路径提供默认兼容和自愈。

## [v0.1.7] - 2026-07-21

### Fixed

- 短信验证码等待超时后停止本轮外层登录重试，避免无人响应时重复请求并发送短信验证码。
- 每日抓取偏移写入 `/data/daily_schedule.json`；容器在午夜后重启时会恢复仍待执行的上一逻辑日跨日任务，避免正偏移或每日两次任务被漏调度。

## [v0.1.6] - 2026-07-20

### Added

- 每日定时抓取在持续运行期间会为下一天重新抽取 `SGCC_DAILY_JITTER_MINUTES` 偏移；每天两次任务仍共用当天偏移并保持 12 小时间隔。
- 新增可配置的 `phone-code,qrcode` 人工登录兜底顺序，以及 Telegram Bot 二维码通知、短信验证码回复和结果通知。
- 新增统一的 `SGCC_LOGIN_FALLBACK_UNATTENDED` 安全开关；默认定时任务不等待人工响应，旧 `SGCC_QRCODE_FALLBACK_UNATTENDED` 保持兼容。
- 新增默认关闭的 `SGCC_RISK_FALLBACK_OVERRIDE`；只有本次任务原本允许 fallback 时，才可在 RK001 后尝试一次已配置的限时人工接管，失败仍进入冷却。

## [v0.1.5] - 2026-07-03

### Changed

- 整理仓库结构：移除 `scripts/` Python 兼容 wrapper，Docker/Add-on/镜像入口统一使用 `sgcc_ha_bridge` 包模块。
- 整理 examples、开发文档和发布文档，补充 Markdown 本地链接检查。

### CI

- 补充包入口导入检查、Markdown 本地链接检查和 Dockerfile 静态检查，文档与示例变更也会触发 CI。

## [v0.1.4] - 2026-06-30

### Fixed

- 修正阿里云 ACR browser-service 镜像发布方式：browser 镜像改为发布到已公开的 `sgcc_ha:browser-*` tag，避免新建 `sgcc_ha_browser` 仓库匿名拉取失败。
- Add-on 版本更新到 `v0.1.4`。

### Docs

- 更新 Docker Compose 国内镜像示例，app 使用 `sgcc_ha:*`，browser 使用 `sgcc_ha:browser-*`。

## [v0.1.3] - 2026-06-30

### Added

- Docker Compose 新增官方 Google Chrome browser-service sidecar 镜像，默认 `SGCC_BROWSER_MODE=browser-service`，主程序通过 CDP attach，Chrome 按需启动并在任务结束后关闭。
- Home Assistant Add-on 默认接入单容器内嵌 browser-service，镜像内安装官方 `google-chrome-stable` 和匹配 ChromeDriver；Add-on 用户不需要另装 Google Chrome。
- CI 同时发布 app 镜像和 browser-service 镜像到 GHCR / 阿里云 ACR。

### Changed

- Add-on 版本更新到 `v0.1.3`。
- `docker-compose.yml` 支持通过 `SGCC_APP_IMAGE` / `SGCC_BROWSER_IMAGE` 直接拉取预构建镜像，也保留本地 build。

### Docs

- 补充 `browser-service` / `local` / `host-cdp` 三种浏览器模式说明，以及 Docker Compose、Add-on 和 RK001 场景下的配置切换方式。

## [v0.1.2] - 2026-06-27

### Fixed

- 修复多户号账号只抓到部分户号的问题：Path B 现在会先抓当前户号，再遍历所有可切换下拉项，并按真实户号去重，避免当前户号被下拉组件隐藏/禁用或同名民用户号被文本去重折叠。

### Changed

- Add-on 版本更新到 `v0.1.2`。

## [v0.1.1] - 2026-06-19

### Added

- 日用电抓取会尽量切换到国网页面近 30 天范围，并把更多日历史发布到 HA/MQTT。
- 新增无人值守登录风控熔断：RK001、操作频繁、验证码通过后仍失败等情况会进入冷却，避免立即重试反复打账号。
- 新增 `SGCC_DAILY_RUNS`、`RISK_COOLDOWN_MINUTES`、`SGCC_LOGIN_COOLDOWN_ENABLED`、`SGCC_QRCODE_FALLBACK_UNATTENDED` 等运行参数。

### Changed

- 无人值守默认每日一次真实登录；二维码兜底默认不用于定时无人值守任务。
- 浏览器启动时补充语言、timezone 与 webdriver 显性特征一致性设置，减少误判风险。

## [arc-v0.1.0] - 2026-06-18

第一版 SGCC Home Assistant Bridge 二开发布。

### Added

- 真实浏览器账号密码登录，支持多模态 LLM 点选验证码。
- Path B 抓取 SGCC Vue2/Vuex store 与组件数据。
- 规范化 SQLite 本地事实源：账户、余额、日/月/年用电、运行记录、会话检查、发布状态。
- Home Assistant MQTT Discovery 自动创建设备和实体，并保留 REST states API 兼容发布。
- 日用电、月度、年度、峰/平/谷/尖分时数据的缓存恢复与重发布。
- 错误现场保存与日志脱敏。

### Changed

- 保留上游 Home Assistant / Docker 部署外壳，重写核心抓取、解析、存储和发布链路。
- 项目元数据、README、Add-on repository 信息改为 SGCC Home Assistant Bridge。

[arc-v0.1.0]: https://github.com/MaribelHearm/sgcc-home-assistant-bridg/releases/tag/arc-v0.1.0
