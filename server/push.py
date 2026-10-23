import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from server.cleanup import delete_device


class PushRegistration(BaseModel):
    provider: Literal['jpush'] = 'jpush'
    registration_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,256}$')
    app_key: str = Field(pattern=r'^[a-fA-F0-9]{24}$')
    manufacturer: str = Field(default='', max_length=80)
    model: str = Field(default='', max_length=100)
    android_version: str = Field(default='', max_length=40)
    app_version: str = Field(default='', max_length=40)


class PushFailure(Exception):
    def __init__(self, reason, retryable=True):
        self.reason, self.retryable = reason, retryable


class JPushProvider:
    def __init__(self, app_key, master_secret, transport=None, third_party_channels=None):
        self.app_key = app_key
        self.secret = master_secret
        self.transport = transport
        self.third_party_channels = third_party_channels

    async def send(self, registration_id, reminder, delivery_id, now):
        start = datetime.fromtimestamp(reminder['start_at'], ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')
        payload = {
            'platform': 'android',
            'audience': {'registration_id': [registration_id]},
            'notification': {'android': {
                'title': '上课提醒：' + reminder['course_name'],
                'alert': f"{start} 开课\n教室：{reminder['room'] or '待定'}",
                'channel_id': 'class_reminder_v2',
                'extras': {'delivery_id': delivery_id, 'start_at': reminder['start_at']},
            }},
            'options': {'time_to_live': max(0, min(86400, reminder['start_at'] - now))},
        }
        if self.third_party_channels:
            payload['options']['third_party_channel'] = self.third_party_channels
        try:
            async with httpx.AsyncClient(timeout=10, transport=self.transport) as client:
                response = await client.post('https://api.jpush.cn/v3/push', json=payload,
                                             auth=(self.app_key, self.secret))
        except httpx.HTTPError:
            raise PushFailure('network_error') from None
        if response.status_code != 200:
            raise PushFailure(f'provider_http_{response.status_code}',
                              response.status_code == 429 or response.status_code >= 500)
        try:
            message_id = str(response.json()['msg_id'])
        except (ValueError, KeyError, TypeError):
            raise PushFailure('invalid_provider_response') from None
        return message_id


def revision(reminder):
    content = [reminder[key] for key in ('course_name', 'room', 'start_at', 'remind_at')]
    return hashlib.sha256(json.dumps(content, ensure_ascii=True).encode()).hexdigest()


class PushDispatcher:
    def __init__(self, db, provider):
        self.db, self.provider = db, provider
        self.lock = asyncio.Lock()
        with db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS push_routes(
                device TEXT PRIMARY KEY, registration_id TEXT NOT NULL,
                app_key TEXT NOT NULL, metadata TEXT NOT NULL,
                UNIQUE(app_key,registration_id));
            CREATE TABLE IF NOT EXISTS push_deliveries(
                id TEXT PRIMARY KEY, device TEXT NOT NULL, reminder TEXT NOT NULL,
                revision TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt INTEGER NOT NULL DEFAULT 0, lease_until INTEGER NOT NULL DEFAULT 0,
                error TEXT, provider_message_id TEXT, updated_at INTEGER NOT NULL,
                UNIQUE(device,reminder,revision));
            ''')

    def bind(self, c, device_id, registration):
        if self.provider and registration.app_key != self.provider.app_key:
            raise HTTPException(409, 'AppKey differs between App and server')
        c.execute('INSERT INTO push_routes VALUES(?,?,?,?) ON CONFLICT(device) DO UPDATE SET '
                  'registration_id=excluded.registration_id,app_key=excluded.app_key,metadata=excluded.metadata',
                  (device_id, registration.registration_id, registration.app_key,
                   registration.model_dump_json(exclude={'registration_id', 'app_key', 'provider'})))

    async def tick(self, now=None):
        if not self.provider:
            return
        use_wall_clock = now is None
        now = int(time.time()) if use_wall_clock else now
        async with self.lock:
            with self.db() as c:
                c.execute("UPDATE push_deliveries SET state='expired',updated_at=? WHERE state IN ('pending','sending') "
                          'AND EXISTS (SELECT 1 FROM reminders r WHERE r.device=push_deliveries.device '
                          'AND r.id=push_deliveries.reminder AND r.start_at<=?)', (now, now))
                rows = c.execute('''SELECT r.*,p.registration_id,p.app_key FROM reminders r
                    JOIN devices d ON d.id=r.device AND d.enabled=1 AND d.execution_mode='server'
                    JOIN push_routes p ON p.device=r.device
                    WHERE r.remind_at<=? AND r.start_at>? ORDER BY r.remind_at''', (now, now)).fetchall()
            for row in rows:
                if use_wall_clock:
                    now = int(time.time())
                if row['start_at'] <= now:
                    continue
                reminder = dict(row)
                if reminder['app_key'] != self.provider.app_key:
                    continue
                rev = revision(reminder)
                delivery_id = hashlib.sha256(json.dumps([row['device'], row['id'], rev]).encode()).hexdigest()
                with self.db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    # Recheck after claiming the transaction: course replacements and revocations cancel queued work.
                    current = c.execute('SELECT r.*,p.registration_id FROM reminders r '
                                        'JOIN devices d ON d.id=r.device AND d.enabled=1 '
                                        'JOIN push_routes p ON p.device=r.device WHERE r.device=? AND r.id=?',
                                        (row['device'], row['id'])).fetchone()
                    if current is None or revision(dict(current)) != rev:
                        continue
                    registration_id = current['registration_id']
                    c.execute("INSERT OR IGNORE INTO push_deliveries(id,device,reminder,revision,state,updated_at) VALUES(?,?,?,?,'pending',?)",
                              (delivery_id, row['device'], row['id'], rev, now))
                    changed = c.execute("UPDATE push_deliveries SET state='sending',attempts=attempts+1,lease_until=?,updated_at=? "
                                        "WHERE id=? AND ((state='pending' AND next_attempt<=?) OR (state='sending' AND lease_until<=?))",
                                        (now + 60, now, delivery_id, now, now)).rowcount
                    if not changed:
                        continue
                    attempts = c.execute('SELECT attempts FROM push_deliveries WHERE id=?', (delivery_id,)).fetchone()[0]
                try:
                    message_id = await self.provider.send(registration_id, reminder, delivery_id, now)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failure = exc if isinstance(exc, PushFailure) else PushFailure('internal_error')
                    with self.db() as c:
                        c.execute('UPDATE push_deliveries SET state=?,error=?,next_attempt=?,updated_at=? WHERE id=?',
                                  ('pending' if failure.retryable else 'rejected', failure.reason,
                                   now + min(300, 5 * 2 ** min(attempts - 1, 6)), now, delivery_id))
                else:
                    with self.db() as c:
                        # Accepted by the provider does NOT mean delivered to the phone.
                        c.execute("UPDATE push_deliveries SET state='accepted',error=NULL,provider_message_id=?,updated_at=? WHERE id=?",
                                  (message_id, now, delivery_id))

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger(__name__).error('Push dispatcher failure: %s', type(exc).__name__)
            await asyncio.sleep(2)


def install_routes(app, db, device, admin, dispatcher):
    @app.put('/devices/me/push')
    def update(registration: PushRegistration, device_id=Depends(device)):
        import sqlite3
        try:
            with db() as c:
                dispatcher.bind(c, device_id, registration)
        except sqlite3.IntegrityError:
            raise HTTPException(409, 'Push registration is already bound to another device') from None
        return {'registered': True, 'push_configured': dispatcher.provider is not None}

    @app.delete('/devices/me')
    def unbind(device_id=Depends(device)):
        with db() as c:
            delete_device(c, device_id)
        return {'unbound': True}

    @app.get('/admin/push-deliveries', dependencies=[Depends(admin)])
    def deliveries():
        with db() as c:
            return [dict(row) for row in c.execute('SELECT id,device,reminder,state,attempts,error,provider_message_id,updated_at '
                                                   'FROM push_deliveries ORDER BY updated_at DESC LIMIT 100')]
