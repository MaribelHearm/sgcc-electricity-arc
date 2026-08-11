# 发布与镜像流程

## 版本来源

正式版本发布前保持这些位置一致：

- `pyproject.toml`：Python 包版本，例如 `0.1.9`。
- `config.yaml`：Home Assistant Add-on/App 版本，例如 `v0.1.9`。
- `CHANGELOG.md` / `DOCS.md` / `ha_addons_doc/`：对外文档中的版本说明。

普通功能或修复 PR 可以先进入 `main` 的 `Unreleased`，不必在同一个 PR 中创建版本 tag。`main` 会发布 `latest`、`main` 和 `sha-*` 镜像；正式 Add-on/App 版本仍通过单独的 `v*` tag 和 GitHub Release 发布。

## CI 输出

PR、`main` 分支和 `v*` tag 会触发 `.github/workflows/docker-image.yml`：

- 跑单元测试、包入口导入检查和 Markdown 本地链接检查。
- 构建 app 镜像：`Dockerfile-for-github-action`。
- 构建 browser-service 镜像：`Dockerfile.browser`。
- PR 只验证、不推镜像。
- 合并到 `main` 或推送 `v*` tag 后推送 GHCR；如果配置了 Aliyun ACR secrets，同步推送 ACR。

## 镜像 tag

GHCR 使用两个仓库：

```text
ghcr.io/maribelhearm/sgcc-home-assistant-bridge
ghcr.io/maribelhearm/sgcc-home-assistant-bridge-browser
```

常用 tag：

```text
latest
main
sha-xxxxxxx
v0.1.9
```

Aliyun ACR 使用同一个公开仓库，browser-service 镜像使用 `browser-*` 前缀：

```text
latest
main
sha-xxxxxxx
v0.1.9
browser-latest
browser-main
browser-sha-xxxxxxx
browser-v0.1.9
```

## 变更 PR 检查清单

```bash
python -m unittest discover -s tests -v
python -m pytest -q
python -m compileall -q sgcc_ha_bridge tests
python tools/check_markdown_links.py
docker build --check -f Dockerfile-for-github-action .
docker build --check -f Dockerfile.browser .
git diff --check
```

确认无误后：

1. 从最新 `main` 创建功能/修复分支，更新代码、测试、文档和 `CHANGELOG.md` 的 `Unreleased`。
2. 推送分支并创建 PR，等待 `test` 和 `docker` 两个 CI job 全部成功。
3. 检查最终 diff、敏感信息和 PR head，确认无冲突后合并到 `main`。
4. 等待 `main` 的 CI 再次完成；该轮会构建并发布 GHCR app/browser 镜像，并在 secrets 可用时发布 ACR。
5. 检查 `latest` / `main` / `sha-*` manifest，确认实际发布结果后再更新相关 issue。

## 正式版本发布

在需要发布新的 Add-on/App 版本时：

1. 更新 `pyproject.toml`、`config.yaml`、`CHANGELOG.md`、`DOCS.md`、`example.env` 和 Add-on 文档中的版本与新增配置；实体身份变更还要同步迁移说明、全部 Lovelace 示例及其契约测试。
2. 通过 PR 合并版本提交，并等待 `main` CI 成功。
3. 在已验证的 `main` commit 上创建并推送 `v*` tag。
4. 等待 tag CI 构建并检查 GHCR / ACR 版本镜像 tag。
5. 创建 GitHub Release，列出变更和镜像地址。
6. 对 Add-on/App 做一次安装或重启 smoke test。

## 回滚

- Docker Compose：把 `.env` 的 `SGCC_APP_IMAGE` / `SGCC_BROWSER_IMAGE` 固定回上一个 tag，然后 `docker compose pull && docker compose up -d`。
- Add-on/App：回滚到上一个 `config.yaml` 版本对应的 GHCR tag，或临时使用上一版仓库提交。
