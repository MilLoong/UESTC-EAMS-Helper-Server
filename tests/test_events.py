import re
import time

from fastapi.testclient import TestClient

from server.app import create_app
from server.dev import create_dev_app
from server.tunnel import device_gateway


ADMIN = {'Authorization': 'Bearer ' + 'a' * 32}


def binding(client, headers=ADMIN):
    code = client.post('/admin/pairings', headers=headers).json()['code']
    response = client.post('/devices/pair', json={'code': code, 'name': 'Phone',
                                               'device_info': {'manufacturer': 'HUAWEI', 'sdk_int': 36}})
    assert response.status_code == 200
    pair = response.json()
    return pair['device_id'], {'Authorization': 'Bearer ' + pair['token']}


def upload(client, auth, now):
    assert client.put('/devices/me/courses', headers=auth, json={
        'lead_minutes': 1, 'courses': [{'id': 'course', 'course_name': 'Math', 'room': 'A101', 'start_at': now + 120}]
    }).status_code == 200


def test_modes_durable_cursor_and_device_isolation(tmp_path):
    path = str(tmp_path / 'db')
    app = create_app(path, 'a' * 32, start_dispatcher=False)
    now = int(time.time())
    with TestClient(app) as client:
        device, auth = binding(client)
        other_device, other = binding(client)
        assert device != other_device
        upload(client, auth, now)
        assert client.put('/devices/me/mode', headers=auth, json={'mode': 'local'}).json()['mode'] == 'local'
        app.state.event_dispatcher.tick(now + 60)
        assert client.get('/devices/me/events', headers=auth).json()['events'] == []
        client.put('/devices/me/mode', headers=auth, json={'mode': 'server'})
        app.state.event_dispatcher.tick(now + 60)
        app.state.event_dispatcher.tick(now + 61)
        events = client.get('/devices/me/events', headers=auth).json()['events']
        assert len(events) == 1 and events[0]['course_name'] == 'Math'
        cursor = events[0]['seq']
        assert client.get(f'/devices/me/events?after={cursor}', headers=auth).json()['events'] == []
        assert client.get('/devices/me/events', headers=other).json()['events'] == []
        paused = client.put('/devices/me/mode', headers=auth, json={'mode': 'local'}).json()
        assert paused['cursor'] == cursor
        assert client.get('/devices/me/events', headers=auth).json()['events'] == []
        assert client.put('/devices/me/mode', headers=auth, json={'mode': 'invalid'}).status_code == 422
    restarted = create_app(path, 'a' * 32, start_dispatcher=False)
    with TestClient(restarted) as client:
        client.put('/devices/me/mode', headers=auth, json={'mode': 'server'})
        restarted.state.event_dispatcher.tick(now + 62)
        assert len(client.get('/devices/me/events', headers=auth).json()['events']) == 1
        assert client.get('/devices/me/events?after=-1', headers=auth).status_code == 422
        client.delete('/admin/devices/' + device, headers=ADMIN)
        assert client.get('/devices/me/events', headers=auth).status_code == 401


def test_dev_page_csrf_and_public_gateway(tmp_path):
    app = create_dev_app(tmp_path, '192.168.1.2', start_dispatcher=False)
    key = (tmp_path / 'admin.key').read_text()
    admin = {'Authorization': 'Bearer ' + key}
    with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 12345)) as client:
        device, auth = binding(client, admin)
        page = client.get('/dev/pair')
        assert page.status_code == 200 and '<svg' in page.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        assert client.post('/dev/notifications/test', data={'device': device}).status_code == 403
        assert client.post('/dev/notifications/test', data={'csrf': csrf, 'device': device},
                           headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.post('/dev/notifications/test', data={'csrf': csrf, 'device': device}, follow_redirects=False).status_code == 303
        events = client.get('/devices/me/events', headers=auth).json()['events']
        assert events[0]['kind'] == 'test'
        assert client.get('/dev/pair', headers={'Host': 'evil.example'}).status_code == 403
    with TestClient(app, base_url='http://192.168.1.2:8000', client=('192.168.1.3', 12345)) as outsider:
        assert outsider.get('/dev/pair').status_code == 403
        assert outsider.get('/admin/devices', headers=admin).status_code == 403
    public = TestClient(device_gateway(app), base_url='https://public.example', client=('127.0.0.1', 12345))
    assert public.get('/health').status_code == 200
    for path in ('/dev/pair', '/admin/devices', '/docs', '/openapi.json'):
        assert public.get(path).status_code == 404
    assert public.get('/devices/me/events', headers=auth).json()['events'][0]['kind'] == 'test'


def test_no_late_events_generated_after_class_started(tmp_path):
    app = create_app(str(tmp_path / 'db'), 'a' * 32, start_dispatcher=False)
    with TestClient(app) as client:
        _, auth = binding(client)
        now = int(time.time())
        upload(client, auth, now)
        app.state.event_dispatcher.tick(now + 121)
        assert client.get('/devices/me/events', headers=auth).json()['events'] == []
