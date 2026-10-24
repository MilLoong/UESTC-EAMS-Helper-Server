# UESTC-EAMS-Helper-Server

将教务助手的 **课程提醒计时放到服务端**，手机接收通知，离线后重新打开应用补取遗漏消息

支持在自己的电脑或云服务器运行，使用 **Cloudflare 临时 HTTPS 隧道 + 客户端扫码绑定**，无需自备域名，手机无需与服务端连接同一 Wi-Fi

## 主要功能 🧩

- **课程提醒：** 手机上传未来 7 天缓存课表，服务器按设置的提前量生成提醒
- **扫码绑定：** 自动填写服务器地址，无需手动输入管理员令牌
- **模式切换：** 支持本地监听和服务器监听，切回本地前等待服务器确认暂停
- **配置同步：** 绑定、启用服务器模式、更新课表和修改提醒提前量时自动同步
- **离线补取：** 通知事件持久保存，手机恢复连接后按游标补取
- **测试通知：** 管理页面可发送测试消息，不上课也能验证接收
- **自动清理：** 启动时及每小时清理过期课程，通知保留 7 天，撤销设备时删除关联数据

## 手机绑定 📱

1. 启动服务端，打开管理页面 `http://127.0.0.1:8000/dev/pair`
2. 安装支持服务器模式的「教务助手」App，登录并刷新课表
3. 在 **我的 → 监听模式** 选择 **服务器监听**，点击 **扫码绑定**
4. 扫描二维码，核对服务器地址和数据范围，确认连接

- **绑定码：** 180 秒有效，仅能使用一次，过期后刷新管理页面
- **自动接收：** 绑定后自动上传课表并启动接收，卡片显示绑定地址，可点击 **重新扫码** 更换绑定
- **恢复使用：** 临时隧道地址变化后，先在管理页撤销旧设备，再重新扫码
- **后台限制：** 使用前台服务轮询，不依赖厂商推送，不保证息屏即时通知，系统限制可能停止接收，重新打开应用后补取
- **课表范围：** 服务器不自行登录教务系统，需要客户端持续同步后续课程
- **信任范围：** 上传设备信息及课程名称、教室、开课时间和提前量，不上传学校密码或 Cookie，只绑定自己的可信服务器
- **关联项目：** [UESTC-EAMS-Helper-APP](https://github.com/MilLoong/UESTC-EAMS-Helper-APP)

## 开发方式 🛠️

需要 Python 3.11 或 3.12，以及 cloudflared

### Windows

- 在项目目录安装依赖并启动
  - 如下：

    ```powershell
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    winget install -e --id Cloudflare.cloudflared
    .\.venv\Scripts\python.exe -m server.dev --public
    ```

### Linux / 云服务器

- 安装 cloudflared 后，在项目目录执行
  - 如下：

    ```bash
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    .venv/bin/python -m server.dev --public --no-browser
    ```

- 云端管理页通过 SSH 转发访问，电脑浏览器打开 `http://127.0.0.1:8000/dev/pair`
  - 如下：

    ```bash
    ssh -N -L 8000:127.0.0.1:8000 <user>@<server-ip>
    ```

- **测试通知：** 扫码后刷新管理页，在对应设备旁点击 **发送测试通知**
- **持续运行：** 保持服务进程和隧道运行，云端可用 systemd 管理，每个数据库只运行一个服务进程
- **状态目录：** `.local-dev/` 保存密钥、数据库与绑定状态，不要删除或公开，不要将管理端口暴露到公网
- **隧道信任：** Cloudflare 终止 HTTPS，可接触绑定凭据、课程与通知数据，临时隧道仅用于可信测试
- **更新生效：** 修改服务端代码后重启进程，临时公网地址可能变化
- **固定 HTTPS 地址：** 复制 `.env.example` 为 `.env`，设置 `EAMS_PUBLIC_URL`、`EAMS_STATE_DIR` 和 `EAMS_PORT`，可使用 `python -m uvicorn server.cloud:from_env --factory --env-file .env --host 127.0.0.1 --port 18080 --workers 1 --no-proxy-headers` 启动，监听端口需与 `EAMS_PORT` 一致
- **反向代理：** 只公开 `/health`、`/devices/pair`、`/devices/me` 及其 `courses`、`reminders`、`mode`、`events` 子路径，将上游 `Host` 设置为 `127.0.0.1:18080`，管理页 `/dev/pair` 和 `/admin/` 仅经 SSH 转发访问，不对公网开放
- **运行测试：** 使用虚拟环境中的 Python 执行
  - 如下：

    ```bash
    python -m pytest -q
    ```
