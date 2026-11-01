# 智子账户与 Token 配置

## 直接用账号登录

在插件设置中填写 `worker.zhizi_account`（手机号或邮箱）和 `worker.zhizi_password`，保存并启用插件即可。插件会向智子换取用户 Token，无需自己复制。

## 单独获取 Token

1. 在 [智子围棋](https://www.zhizigo.com/) 注册或登录自己的账户。
2. 下载本仓库到电脑，安装 Python。在仓库目录运行 `python -m pip install httpx`。
3. 首次使用时，把 `deploy/.env.example` 复制为 `deploy/.env`；已有配置文件请保留。
4. 运行 `python tools/zhizi_login.py`，输入智子手机号或邮箱及密码，密码输入时不显示。
5. 成功后打开本机的 `deploy/.env`，找到 `ZHIZI_USER_TOKEN=`，复制等号后面的内容。
6. 在 MaiBot 插件设置中，将它填入 `worker.zhizi_token`，保存并启用。使用 Token 时，账号和密码可以留空。

登录工具只获取并保存账户 Token，不会启动算力。请保管好 Token 与配置文件，不要发到群里或上传 GitHub。这里需要的是账户用户 Token，不是临时引擎会话 Token。

## 开始使用

在 `plugin.allowed_groups` 填允许使用的群号，开启 `plugin.enabled`。然后在群里 @机器人并发送野狐公开棋谱链接。

首次启动会准备出图环境，完成后便可接收任务。云端分析使用自己的智子账户余额。
