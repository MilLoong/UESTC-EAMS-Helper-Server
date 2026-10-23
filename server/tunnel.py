"""Expose only phone endpoints through a temporary HTTPS tunnel."""
import asyncio
import os
import re
import socket
import subprocess
from contextlib import nullcontext

import uvicorn
from fastapi.responses import JSONResponse


def device_gateway(app):
    async def gateway(scope, receive, send):
        allowed = {'/health', '/devices/pair', '/devices/me', '/devices/me/courses',
                   '/devices/me/reminders', '/devices/me/mode', '/devices/me/events'}
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
        elif scope['type'] == 'http':
            if scope.get('path') not in allowed:
                await JSONResponse({'detail': 'not_found'}, status_code=404)(scope, receive, send)
            else:
                # Do not expose the local admin boundary via a forged Host header.
                forwarded = dict(scope)
                forwarded['headers'] = [(k, v) for k, v in scope['headers'] if k.lower() != b'host'] + [(b'host', b'127.0.0.1')]
                await app(forwarded, receive, send)
    return gateway


class QuickTunnel:
    def __init__(self, app, executable):
        self.app, self.executable = app, executable
        self.process = self.server = self.task = self.reader = self.sock = None

    async def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(128)
        self.sock.setblocking(False)
        port = self.sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(device_gateway(self.app), host='127.0.0.1', port=port,
                                                   lifespan='off', proxy_headers=False, access_log=False, log_level='warning'))
        self.server.capture_signals = lambda: nullcontext()
        self.task = asyncio.create_task(self.server.serve(sockets=[self.sock]))
        while not self.server.started:
            if self.task.done():
                await self.task
                raise RuntimeError('Device gateway failed to start')
            await asyncio.sleep(0.05)
        self.process = await asyncio.create_subprocess_exec(
            self.executable, 'tunnel', '--no-autoupdate', '--url', f'http://127.0.0.1:{port}', '--protocol', 'http2',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

        async def find_url():
            while line := await self.process.stdout.readline():
                match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com(?=[\s"<>|]|$)', line.decode(errors='replace'))
                if match:
                    return match.group(0)
            raise RuntimeError('cloudflared exited before producing a URL')

        try:
            url = await asyncio.wait_for(find_url(), 90)
        except BaseException:
            await self.close()
            raise
        self.reader = asyncio.create_task(self.drain())
        return url

    async def drain(self):
        while await self.process.stdout.readline():
            pass
        self.app.state.public_origin = None
        self.app.state.tunnel_error = '隧道已断开，请重新启动 --public 测试'

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            try:
                await self.reader
            except asyncio.CancelledError:
                pass
        if self.server:
            self.server.should_exit = True
        if self.task:
            await self.task
        if self.sock:
            self.sock.close()
