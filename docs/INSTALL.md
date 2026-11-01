# 安装指南

1. 在 MaiBot 插件市场安装阿羽围棋复盘，或使用仓库地址安装。
2. 打开插件配置，在 `worker.zhizi_account` 填写智子手机号或邮箱，在 `worker.zhizi_password` 填写密码；如果已经有智子用户 Token，也可以只填写 `worker.zhizi_token`。请妥善保管插件配置，密码和 Token 都属于账户凭据。
3. 在 `plugin.allowed_groups` 填写允许使用的群号，开启 `plugin.enabled` 并保存。插件会自动启动内置复盘服务；首次使用时如缺少浏览器，会尝试自动下载。
4. 在群里 @ 机器人并发送野狐公开棋谱链接。

云端分析使用自己的智子账户余额。首次下载浏览器需要网络；如果系统缺少浏览器运行所需的系统库，请按系统环境安装。Token 无法获取时，可用仓库中的 `python tools/zhizi_login.py` 登录并写入 `deploy/.env`，再将文件中的 `ZHIZI_USER_TOKEN` 填入插件的 `worker.zhizi_token`。请先复制 `deploy/.env.example` 为 `deploy/.env`，并安装 Python 与 httpx；不要分享该文件。

## 已有独立 Worker 的用户

原有 `worker.token` 配置会自动沿用独立 Worker。需要手动指定时，可将 `worker.mode` 设为 `external`；首次安装默认 `auto`，无需另行部署 Worker。`embedded` 可强制使用内置服务。

已有环境通过 MaiBot 1.3.15 / SDK 2.16.0 接入。Docker 独立 Worker 示例仍保留在 `deploy/`。

