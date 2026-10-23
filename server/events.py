import asyncio
import json
import logging
import time

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel

from server.push import revision


class Mode(BaseModel):
    mode: str


class EventDispatcher:
    """Durable server-side reminders; phones independently catch up using a cursor."""

    def __init__(self, db):
        self.db = db
        with db() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS notification_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT NOT NULL,
                reminder TEXT NOT NULL, revision TEXT NOT NULL, payload TEXT NOT NULL,
                created INTEGER NOT NULL, UNIQUE(device,reminder,revision))''')

    def tick(self, now=None):
        now = int(time.time()) if now is None else now
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            rows = c.execute('SELECT r.* FROM reminders r JOIN devices d ON d.id=r.device '
                             "WHERE d.enabled=1 AND d.execution_mode='server' AND r.remind_at<=? AND r.start_at>?",
                             (now, now)).fetchall()
            for row in rows:
                payload = dict(row)
                del payload['device']
                payload['kind'] = 'course'
                c.execute('INSERT OR IGNORE INTO notification_events(device,reminder,revision,payload,created) VALUES(?,?,?,?,?)',
                          (row['device'], row['id'], revision(dict(row)), json.dumps(payload), now))

    async def run(self):
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger(__name__).error('Event dispatch failed: %s', type(exc).__name__)
            await asyncio.sleep(2)


def install_routes(app, db, device, admin):
    @app.put('/devices/me/mode')
    def switch_mode(body: Mode, device_id=Depends(device)):
        if body.mode not in ('local', 'server'):
            raise HTTPException(422, 'mode must be local or server')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE devices SET execution_mode=? WHERE id=? AND enabled=1', (body.mode, device_id))
            cursor = c.execute('SELECT COALESCE(MAX(seq),0) FROM notification_events WHERE device=?', (device_id,)).fetchone()[0]
        return {'mode': body.mode, 'cursor': cursor}

    @app.get('/devices/me/events')
    def events(after: int = Query(default=0, ge=0), device_id=Depends(device)):
        with db() as c:
            mode = c.execute('SELECT execution_mode FROM devices WHERE id=?', (device_id,)).fetchone()[0]
            rows = c.execute('SELECT seq,payload,created FROM notification_events WHERE device=? AND seq>? ORDER BY seq LIMIT 100',
                             (device_id, after)).fetchall() if mode == 'server' else []
        return {'mode': mode, 'events': [json.loads(row['payload']) | {'seq': row['seq'], 'created': row['created']} for row in rows]}

    @app.get('/admin/notification-events', dependencies=[Depends(admin)])
    def event_history():
        with db() as c:
            return [dict(row) for row in c.execute('SELECT seq,device,reminder,created FROM notification_events ORDER BY seq DESC LIMIT 100')]
