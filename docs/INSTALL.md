# 安装指南

阿羽由 MaiBot 插件和独立 Worker 组成。请先备份原有服务，在测试环境配置。

1. 将 `maibot_goreview` 目录放进 MaiBot 插件扫描目录，保证目录内直接有 `_manifest.json` 和 `plugin.py`。
2. 使用 `deploy/Dockerfile` 构建 Worker，或用 Python 3.12+ 安装根目录项目与 Playwright Chromium。Worker 只运行一个进程，数据目录需要持久化；不要将 Worker 端口公开到公网。
3. Worker 设置 `ZHIZI_USER_TOKEN`（智子账户令牌）、随机且至少 32 字符的 `GOREVIEW_WORKER_TOKEN`，两者通过私有环境变量或配置管理器保存。插件的 `worker.token` 使用同一个 Worker Token，`worker.url` 指向 Worker 的内网地址。不要将密钥提交至仓库。
4. 在确认账户费用、规则及引擎视角配置后，设置 `GOREVIEW_ALLOW_GPU=true`、`ZHIZI_PERSPECTIVE_VERIFIED=true`、`ZHIZI_KOMI_VERIFIED=true`。两个 VERIFIED 开关应在自己的环境确认后才开启。可设置 `GOREVIEW_SESSION_SECONDS=160`、`GOREVIEW_SCAN_VISITS=8`、`GOREVIEW_CONCURRENCY=1`。
5. 在插件配置里开启 `plugin.enabled`，并在 `plugin.allowed_groups` 填允许使用的群号。示例见 `maibot_goreview/config.example.toml`。启用插件后，在测试群 @机器人并发送野狐公开分享链接。

此项目在 MaiBot 1.3.15 / SDK 2.16.0 实测接入，其他版本尚未逐一验证。智子 Token、插件连接 Worker 的 Token 不是同一个。部署与费用由安装者管理。

Docker Compose 示例：先将 `deploy/.env.example` 复制为 `deploy/.env` 并填写，再在仓库根目录运行：

```bash
docker compose --env-file deploy/.env -f deploy/compose.example.yml up -d --build
```

`MAIBOT_NETWORK` 填 MaiBot 核心容器实际使用的 Docker 网络名称；示例不会对公网发布 Worker 端口。`deploy/.env` 含密钥，注意文件访问权限。
