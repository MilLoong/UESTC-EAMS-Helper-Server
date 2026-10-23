import asyncio
import hashlib
import io
import json
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from urllib.parse import urlsplit
from ipaddress import IPv4Address, IPv4Network

import qrcode
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from server.reminders import install_routes
from server.push import JPushProvider, PushDispatcher, PushRegistration, install_routes as install_push_routes
from server.events import EventDispatcher, install_routes as install_event_routes
from server.cleanup import CacheCleanup, delete_device


class PairRequest(BaseModel):
    code: str = Field(min_length=32, max_length=128)
    name: str = Field(min_length=1, max_length=80)
    push: PushRegistration | None = None
    device_info: dict[str, str | int] = Field(default_factory=dict, max_length=20)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def create_app(db_path=None, admin_token=None, push_provider=None, start_dispatcher=True):
    db_path = db_path or os.environ.get('EAMS_DB', 'data/server.db')
    admin_token = admin_token or os.environ.get('EAMS_ADMIN_TOKEN', '')
    if len(admin_token) < 32:
        raise RuntimeError('Set EAMS_ADMIN_TOKEN (32+ characters)')
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

    @contextmanager
    def db():
        connection = sqlite3.connect(db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS pairs(code TEXT PRIMARY KEY, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS devices(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT UNIQUE NOT NULL,
            session BLOB, enabled INTEGER NOT NULL DEFAULT 1, error TEXT);
        UPDATE devices SET session=NULL;
        ''')
        # Preserve compatibility with the initial prototype DB, without retaining credentials.
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
            c.execute('DELETE FROM tasks')
        columns = {row['name'] for row in c.execute('PRAGMA table_info(devices)')}
        if 'execution_mode' not in columns:
            c.execute("ALTER TABLE devices ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'local'")
        if 'device_info' not in columns:
            c.execute("ALTER TABLE devices ADD COLUMN device_info TEXT NOT NULL DEFAULT '{}'")

    app_key = os.environ.get('JPUSH_APP_KEY', '')
    master_secret = os.environ.get('JPUSH_MASTER_SECRET', '')
    if push_provider is None and os.environ.get('EAMS_TRANSPORT') == 'jpush' and (app_key or master_secret):
        if not app_key or not master_secret:
            raise RuntimeError('Configure both JPUSH_APP_KEY and JPUSH_MASTER_SECRET')
        third_party = json.loads(os.environ.get('JPUSH_THIRD_PARTY_CHANNELS', '{}'))
        if not isinstance(third_party, dict):
            raise RuntimeError('JPUSH_THIRD_PARTY_CHANNELS must be a JSON object')
        push_provider = JPushProvider(app_key, master_secret, third_party_channels=third_party)
    dispatcher = PushDispatcher(db, push_provider)
    event_dispatcher = EventDispatcher(db)
    cleanup = CacheCleanup(db)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(cleanup.tick)
        task = asyncio.create_task(dispatcher.run()) if start_dispatcher and push_provider else None
        event_task = asyncio.create_task(event_dispatcher.run()) if start_dispatcher else None
        cleanup_task = asyncio.create_task(cleanup.run()) if start_dispatcher else None
        try:
            yield
        finally:
            for pending in (task, event_task, cleanup_task):
                if not pending:
                    continue
                pending.cancel()
                try:
                    await pending
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title='UESTC EAMS Course Reminder Server', version='0.3.0', lifespan=lifespan)
    app.state.dispatcher = dispatcher
    app.state.event_dispatcher = event_dispatcher
    app.state.db = db
    app.state.cleanup = cleanup

    @app.middleware('http')
    async def no_cache(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    def admin(authorization: str = Header(default='')):
        if not secrets.compare_digest(authorization, 'Bearer ' + admin_token):
            raise HTTPException(401, 'Invalid administrator credentials')

    def device(authorization: str = Header(default='')):
        if not authorization.startswith('Bearer '):
            raise HTTPException(401)
        with db() as c:
            row = c.execute('SELECT id FROM devices WHERE token=? AND enabled=1',
                            (digest(authorization[7:]),)).fetchone()
        if row is None:
            raise HTTPException(401)
        return row['id']

    @app.get('/health')
    def health():
        return {'status': 'ok', 'mode': 'server-notifications', 'protocol': 4, 'transport': 'polling',
                'push_configured': push_provider is not None}

    @app.post('/admin/pairings', dependencies=[Depends(admin)])
    def pairing():
        code = secrets.token_urlsafe(32)
        expires = int(time.time()) + 180
        with db() as c:
            c.execute('DELETE FROM pairs WHERE expires<=?', (int(time.time()),))
            c.execute('INSERT INTO pairs VALUES(?,?)', (digest(code), expires))
        result = {'code': code, 'expires_at': expires, 'protocol': 4}
        if os.environ.get('EAMS_PUBLIC_URL'):
            result['server'] = os.environ['EAMS_PUBLIC_URL'].rstrip('/')
        return result

    @app.post('/admin/pairings/qr', dependencies=[Depends(admin)])
    def pairing_qr():
        public_url = os.environ.get('EAMS_PUBLIC_URL', '')
        parts = urlsplit(public_url)
        allow_http = False
        if os.environ.get('EAMS_ALLOW_LAN_HTTP') == '1':
            try:
                ip = IPv4Address(parts.hostname or '')
                allow_http = any(ip in IPv4Network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
            except ValueError:
                pass
        if (parts.scheme != 'https' and not (parts.scheme == 'http' and allow_http)) or not parts.hostname or parts.username or parts.query or parts.fragment:
            raise HTTPException(503, 'Configure EAMS_PUBLIC_URL; LAN HTTP additionally requires EAMS_ALLOW_LAN_HTTP=1')
        payload = pairing() | {'server': public_url, 'local_dev': parts.scheme == 'http'}
        output = io.BytesIO()
        qrcode.make(json.dumps(payload)).save(output, format='PNG')
        return Response(output.getvalue(), media_type='image/png')

    @app.post('/devices/pair')
    def pair(body: PairRequest):
        token, device_id = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
        try:
            with db() as c:
                changed = c.execute('DELETE FROM pairs WHERE code=? AND expires>?',
                                    (digest(body.code), int(time.time()))).rowcount
                if not changed:
                    raise HTTPException(400, 'Pairing code expired or already used')
                info = json.dumps(body.device_info)
                if len(info) > 4096:
                    raise HTTPException(413, 'Device information too large')
                c.execute("INSERT INTO devices(id,name,token,execution_mode,device_info) VALUES(?,?,?,'server',?)",
                          (device_id, body.name, digest(token), info))
                if body.push:
                    dispatcher.bind(c, device_id, body.push)
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'Push registration is already bound to another device') from None
        return {'device_id': device_id, 'token': token, 'push_registered': body.push is not None,
                'push_configured': push_provider is not None, 'mode': 'server', 'protocol': 4}

    @app.get('/admin/devices', dependencies=[Depends(admin)])
    def devices():
        with db() as c:
            return [dict(row) for row in c.execute('SELECT d.id,d.name,d.enabled,d.execution_mode,d.device_info,p.provider_set,p.metadata FROM devices d '
                                                   'LEFT JOIN (SELECT device,1 AS provider_set,metadata FROM push_routes) p ON p.device=d.id')]

    @app.delete('/admin/devices/{device_id}', dependencies=[Depends(admin)])
    def revoke(device_id: str):
        with db() as c:
            delete_device(c, device_id)
        return {'revoked': True, 'note': 'Already submitted provider notifications cannot be recalled by this API'}

    install_routes(app, db, device, admin)
    install_push_routes(app, db, device, admin, dispatcher)
    install_event_routes(app, db, device, admin)
    return app


def from_env():
    return create_app()
