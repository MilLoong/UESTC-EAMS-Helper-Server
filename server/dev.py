"""Local pairing page and optional Cloudflare HTTPS tunnel, following the RainClassroom server."""
import argparse
import hmac
import html
import json
import secrets
import shutil
import socket
import time
import webbrowser
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from urllib.parse import parse_qs

import qrcode
import qrcode.image.svg
import uvicorn
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from server.app import create_app, digest
from server.cleanup import delete_device


def is_lan(address):
    try:
        ip = IPv4Address(address)
        return any(ip in IPv4Network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8'))
    except ValueError:
        return False


def detect_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(('192.0.2.1', 9))
        address = sock.getsockname()[0]
    if not is_lan(address):
        raise RuntimeError('Pass --ip with the LAN IPv4 address')
    return address


def create_dev_app(directory, ip='127.0.0.1', port=8000, public=False, start_dispatcher=True):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key_path = directory / 'admin.key'
    try:
        with key_path.open('x', encoding='ascii') as stream:
            stream.write(secrets.token_urlsafe(32))
        key_path.chmod(0o600)
    except FileExistsError:
        pass
    if not is_lan(ip):
        raise ValueError('LAN IP required')
    app = create_app(str(directory / 'eams.db'), key_path.read_text().strip(), start_dispatcher=start_dispatcher)
    app.state.public_origin = None
    app.state.tunnel_error = None
    csrf = secrets.token_urlsafe(32)
    hosts = {ip, f'{ip}:{port}', '127.0.0.1', f'127.0.0.1:{port}', 'localhost', f'localhost:{port}'}

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        peer = request.client.host if request.client else ''
        if request.headers.get('host', '').lower() not in hosts:
            return JSONResponse({'detail': 'invalid_host'}, status_code=403)
        if request.url.path.startswith(('/dev/', '/admin/')) and peer not in ('127.0.0.1', '::1'):
            return JSONResponse({'detail': 'admin_is_loopback_only'}, status_code=403)
        return await call_next(request)

    @app.get('/dev/pair', response_class=HTMLResponse, include_in_schema=False)
    def page(request: Request):
        origin = app.state.public_origin if public else f'http://{ip}:{port}'
        if not origin:
            return HTMLResponse('<meta charset="utf-8"><meta http-equiv="refresh" content="3">' +
                                html.escape(app.state.tunnel_error or '正在获取临时 HTTPS 地址…'))
        code = secrets.token_urlsafe(32)
        now = int(time.time())
        with app.state.db() as c:
            c.execute('DELETE FROM pairs WHERE expires<=?', (now,))
            c.execute('INSERT INTO pairs VALUES(?,?)', (digest(code), now + 180))
            devices = c.execute('SELECT id,name,execution_mode FROM devices WHERE enabled=1').fetchall()
        qr = qrcode.make(json.dumps({'protocol': 4, 'server': origin, 'code': code, 'expires_at': now + 180,
                                     'local_dev': not public}), image_factory=qrcode.image.svg.SvgPathImage)
        svg = qr.to_string().decode()
        forms = ''.join(f'<form method="post"><input type="hidden" name="csrf" value="{csrf}">'
                        f'<input type="hidden" name="device" value="{html.escape(d["id"], quote=True)}">'
                        f'{html.escape(d["name"])} · {html.escape(d["execution_mode"])} '
                        '<button formaction="/dev/notifications/test">发送测试通知</button>'
                        '<button formaction="/dev/devices/revoke">撤销</button></form>' for d in devices)
        warning = ('经 Cloudflare 中转 HTTPS，第三方可接触课程、绑定凭据和通知内容，临时地址重启后可能变化。'
                   if public else '仅限可信局域网，HTTP 明文传输绑定凭据和课程。')
        queued = '<p>测试通知已入队，请查看手机。入队不代表手机已经显示或震动。</p>' if request.query_params.get('queued') else ''
        return HTMLResponse(f'''<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>教务助手服务器配对</title><style>body{{font:17px system-ui;max-width:760px;margin:32px auto;padding:16px}}
svg{{width:300px;height:300px;max-width:100%}}form{{margin:16px 0}}code{{overflow-wrap:anywhere}}</style>
<h1>教务助手：扫码连接服务器</h1><p><code>{html.escape(origin)}</code></p>{svg}
<p>二维码有效 180 秒，每次仅能使用一次。App → 我的 → 提醒模式 → 扫码连接。</p>
<p>服务器保存提醒事件，手机在线时轮询接收，离线后补取，不使用厂商推送。息屏可能延迟，Android 15+ 前台同步有时长限制。</p>
<h2>设备与测试通知</h2>{queued}{forms or '<p>暂无设备，先扫码绑定，再刷新页面。</p>'}
<p>{warning}</p><p>管理页面仅在电脑本机开放；保持服务进程运行。测试通知不需要上课。</p>''',
                            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin',
                                     'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"})

    async def form(request):
        origin = request.headers.get('origin')
        if origin and origin not in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            raise HTTPException(403, 'invalid_origin')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise HTTPException(413)
        try:
            fields = parse_qs(raw.decode('utf-8'), max_num_fields=5)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, 'invalid_form') from None
        if not hmac.compare_digest(fields.get('csrf', [''])[0].encode(), csrf.encode()):
            raise HTTPException(403, 'invalid_form_token')
        return fields.get('device', [''])[0]

    @app.post('/dev/notifications/test', include_in_schema=False)
    async def test(request: Request):
        device = await form(request)
        now, key = int(time.time()), 'test-' + secrets.token_hex(12)
        payload = {'id': key, 'kind': 'test', 'title': '教务助手：服务器测试通知',
                   'body': '已收到服务器消息，不依赖上课，也没有注册本地闹钟。'}
        with app.state.db() as c:
            row = c.execute('SELECT execution_mode FROM devices WHERE id=? AND enabled=1', (device,)).fetchone()
            if not row or row[0] != 'server':
                raise HTTPException(409, 'Enable server mode on the phone first')
            c.execute('INSERT INTO notification_events(device,reminder,revision,payload,created) VALUES(?,?,?,?,?)',
                      (device, key, key, json.dumps(payload), now))
        return RedirectResponse('/dev/pair?queued=1', status_code=303)

    @app.post('/dev/devices/revoke', include_in_schema=False)
    async def revoke(request: Request):
        device = await form(request)
        with app.state.db() as c:
            delete_device(c, device)
        return RedirectResponse('/dev/pair', status_code=303)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public', action='store_true')
    parser.add_argument('--ip')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--cloudflared')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Invalid port')
    executable = args.cloudflared or shutil.which('cloudflared')
    if args.public and not executable:
        parser.error('Install cloudflared first, or use LAN mode without --public')
    ip = '127.0.0.1' if args.public else (args.ip or detect_ip())
    app = create_dev_app(Path.cwd() / '.local-dev', ip, args.port, args.public)
    local = f'http://127.0.0.1:{args.port}/dev/pair'

    class DevServer(uvicorn.Server):
        tunnel = None

        async def startup(self, sockets=None):
            await super().startup(sockets)
            if self.started and args.public:
                from server.tunnel import QuickTunnel
                self.tunnel = QuickTunnel(app, executable)
                try:
                    app.state.public_origin = await self.tunnel.start()
                except Exception:
                    app.state.tunnel_error = '无法创建隧道，请检查 cloudflared 与网络'
            if self.started and not args.no_browser:
                webbrowser.open(local)

        async def shutdown(self, sockets=None):
            if self.tunnel:
                await self.tunnel.close()
            await super().shutdown(sockets)
    print('Open:', local)
    DevServer(uvicorn.Config(app, host='127.0.0.1' if args.public else '0.0.0.0', port=args.port,
                            proxy_headers=False, access_log=False)).run()


if __name__ == '__main__':
    main()
