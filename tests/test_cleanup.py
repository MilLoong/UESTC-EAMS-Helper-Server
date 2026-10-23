import re
import time

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.cleanup import RETENTION_SECONDS
from server.dev import create_dev_app


ADMIN = {'Authorization': 'Bearer ' + 'a' * 32}


def bind(client, admin=ADMIN):
    code = client.post('/admin/pairings', headers=admin).json()['code']
    result = client.post('/devices/pair', json={'code': code, 'name': 'Phone'}).json()
    return result['device_id'], {'Authorization': 'Bearer ' + result['token']}


def seed(app, device, now):
    with app.state.db() as c:
        c.execute('INSERT INTO reminders VALUES(?,?,?,?,?,?)', (device, 'old', 'Old', '', now, now - 60))
        c.execute('INSERT INTO reminders VALUES(?,?,?,?,?,?)', (device, 'future', 'Future', '', now + 60, now))
        for key, created in [('old', now - RETENTION_SECONDS), ('fresh', now - RETENTION_SECONDS + 1)]:
            c.execute('INSERT INTO notification_events(device,reminder,revision,payload,created) VALUES(?,?,?,?,?)',
                      (device, key, key, '{}', created))
        c.execute('INSERT INTO push_routes VALUES(?,?,?,?)', (device, device, 'key', '{}'))
        c.execute('INSERT INTO push_deliveries(id,device,reminder,revision,state,updated_at) VALUES(?,?,?,?,?,?)',
                  (device, device, 'old', 'revision', 'sent', now - RETENTION_SECONDS))


def test_retention_boundaries_and_cursor_sequence(tmp_path):
    app = create_app(str(tmp_path / 'db'), 'a' * 32, start_dispatcher=False)
    with TestClient(app) as client:
        device, _ = bind(client)
        now = int(time.time())
        seed(app, device, now)
        with app.state.db() as c:
            cursor = c.execute('SELECT MAX(seq) FROM notification_events').fetchone()[0]
        app.state.cleanup.tick(now)
        app.state.cleanup.tick(now)
        with app.state.db() as c:
            assert [r['id'] for r in c.execute('SELECT id FROM reminders')] == ['future']
            assert [r['reminder'] for r in c.execute('SELECT reminder FROM notification_events')] == ['fresh']
            assert c.execute('SELECT COUNT(*) FROM push_deliveries').fetchone()[0] == 0
            c.execute('INSERT INTO notification_events(device,reminder,revision,payload,created) VALUES(?,?,?,?,?)',
                      (device, 'next', 'next', '{}', now))
            assert c.execute('SELECT MAX(seq) FROM notification_events').fetchone()[0] > cursor


@pytest.mark.parametrize('endpoint', ['admin', 'device', 'dev'])
def test_all_revoke_routes_remove_device_data(tmp_path, endpoint):
    if endpoint == 'dev':
        app = create_dev_app(tmp_path, '192.168.1.2', start_dispatcher=False)
        admin = {'Authorization': 'Bearer ' + (tmp_path / 'admin.key').read_text()}
    else:
        app = create_app(str(tmp_path / 'db'), 'a' * 32, start_dispatcher=False)
        admin = ADMIN
    with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 1234)) as client:
        device, auth = bind(client, admin)
        other, other_auth = bind(client, admin)
        seed(app, device, int(time.time()))
        if endpoint == 'dev':
            csrf = re.search(r'name="csrf" value="([^"]+)"', client.get('/dev/pair').text).group(1)
            response = client.post('/dev/devices/revoke', data={'csrf': csrf, 'device': device}, follow_redirects=False)
            assert response.status_code == 303
        elif endpoint == 'admin':
            assert client.delete('/admin/devices/' + device, headers=admin).status_code == 200
        else:
            assert client.delete('/devices/me', headers=auth).status_code == 200
        assert client.get('/devices/me/events', headers=auth).status_code == 401
        assert client.get('/devices/me/events', headers=other_auth).status_code == 200
        with app.state.db() as c:
            for table in ('reminders', 'notification_events', 'push_routes', 'push_deliveries'):
                assert c.execute(f'SELECT COUNT(*) FROM {table} WHERE device=?', (device,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM devices WHERE id=?', (device,)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM devices WHERE id=?', (other,)).fetchone()[0] == 1


def test_startup_removes_expired_and_legacy_revoked_data(tmp_path):
    path = str(tmp_path / 'db')
    app = create_app(path, 'a' * 32, start_dispatcher=False)
    with TestClient(app) as client:
        device, _ = bind(client)
        seed(app, device, int(time.time()))
        with app.state.db() as c:
            c.execute('UPDATE devices SET enabled=0 WHERE id=?', (device,))
            c.execute('INSERT INTO pairs VALUES(?,?)', ('expired', 0))
    restarted = create_app(path, 'a' * 32, start_dispatcher=False)
    with TestClient(restarted):
        with restarted.state.db() as c:
            for table in ('devices', 'reminders', 'notification_events', 'push_routes', 'push_deliveries', 'pairs'):
                assert c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
