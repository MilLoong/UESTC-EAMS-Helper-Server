import asyncio
import logging
import time


RETENTION_SECONDS = 7 * 86400
CLEANUP_INTERVAL = 3600


def delete_device(connection, device_id):
    """Remove the binding and all device-owned records in the caller's transaction."""
    for table in ('reminders', 'notification_events', 'push_routes', 'push_deliveries'):
        connection.execute(f'DELETE FROM {table} WHERE device=?', (device_id,))
    connection.execute('DELETE FROM devices WHERE id=?', (device_id,))


class CacheCleanup:
    def __init__(self, db):
        self.db = db

    def tick(self, now=None):
        now = int(time.time()) if now is None else now
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            for row in c.execute('SELECT id FROM devices WHERE enabled=0').fetchall():
                delete_device(c, row['id'])
            c.execute('DELETE FROM reminders WHERE start_at<=?', (now,))
            c.execute('DELETE FROM notification_events WHERE created<=?', (now - RETENTION_SECONDS,))
            c.execute('DELETE FROM push_deliveries WHERE updated_at<=?', (now - RETENTION_SECONDS,))
            c.execute('DELETE FROM pairs WHERE expires<=?', (now,))

    async def run(self):
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            try:
                await asyncio.to_thread(self.tick)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception('Cache cleanup failed')
