import sqlite3
import time

from fastapi.testclient import TestClient

from server.app import create_app


def test_pair_plan_and_revoke(tmp_path):
    path = str(tmp_path / 'db')
    admin = {'Authorization': 'Bearer ' + 'a' * 32}
    with TestClient(create_app(path, 'a' * 32)) as client:
        def pair():
            code = client.post('/admin/pairings', headers=admin).json()['code']
            reply = client.post('/devices/pair', json={'code': code, 'name': 'Android'})
            assert reply.headers['cache-control'] == 'no-store'
            assert client.post('/devices/pair', json={'code': code, 'name': 'Replay'}).status_code == 400
            return reply.json()
        assert client.post('/admin/pairings').status_code == 401
        first, other = pair(), pair()
        auth = {'Authorization': 'Bearer ' + first['token']}
        other_auth = {'Authorization': 'Bearer ' + other['token']}
        start = int(time.time()) + 3600
        plan = {'lead_minutes': 20, 'courses': [{'id': 'course1', 'course_name': 'Math', 'room': 'A101', 'start_at': start}]}
        assert client.put('/devices/me/courses', headers=auth, json=plan).json() == {'saved': 1}
        assert client.get('/devices/me/reminders', headers=other_auth).json()['reminders'] == []
        reminder = client.get('/devices/me/reminders', headers=auth).json()['reminders'][0]
        assert reminder['remind_at'] == start - 1200
        assert reminder['room'] == 'A101'
        client.put('/devices/me/courses', headers=auth, json=plan)
        assert len(client.get('/devices/me/reminders', headers=auth).json()['reminders']) == 1
        assert client.post('/admin/devices/' + first['device_id'] + '/test-reminder', headers=admin).status_code == 200
        assert len(client.get('/devices/me/reminders', headers=auth).json()['reminders']) == 2
        plan['courses'][0]['start_at'] = 1
        assert client.put('/devices/me/courses', headers=auth, json=plan).status_code == 422
        assert len(client.get('/devices/me/reminders', headers=auth).json()['reminders']) == 2
        client.delete('/admin/devices/' + first['device_id'], headers=admin)
        assert client.get('/devices/me/reminders', headers=auth).status_code == 401
    with TestClient(create_app(path, 'a' * 32)) as restarted:
        assert restarted.get('/devices/me/reminders', headers=other_auth).status_code == 200


def test_expired_pairing(tmp_path):
    path = str(tmp_path / 'db')
    admin = {'Authorization': 'Bearer ' + 'a' * 32}
    with TestClient(create_app(path, 'a' * 32)) as client:
        code = client.post('/admin/pairings', headers=admin).json()['code']
        with sqlite3.connect(path) as c:
            c.execute('UPDATE pairs SET expires=0')
        assert client.post('/devices/pair', json={'code': code, 'name': 'Android'}).status_code == 400


def test_plan_replace_expiry_clear_and_qr(tmp_path, monkeypatch):
    path = str(tmp_path / 'db')
    admin = {'Authorization': 'Bearer ' + 'a' * 32}
    monkeypatch.setenv('EAMS_PUBLIC_URL', 'https://test.example')
    with TestClient(create_app(path, 'a' * 32)) as client:
        qr = client.post('/admin/pairings/qr', headers=admin)
        assert qr.status_code == 200 and qr.content.startswith(b'\x89PNG')
        code = client.post('/admin/pairings', headers=admin).json()['code']
        pair = client.post('/devices/pair', json={'code': code, 'name': 'Phone'}).json()
        auth = {'Authorization': 'Bearer ' + pair['token']}
        now = int(time.time())
        course = {'id': 'c', 'course_name': 'Physics', 'start_at': now + 3600}
        duplicate = {'courses': [course, course]}
        assert client.put('/devices/me/courses', headers=auth, json=duplicate).status_code == 422
        assert client.put('/devices/me/courses', headers=auth, json={'courses': [course]}).status_code == 200
        with sqlite3.connect(path) as c:
            c.execute('UPDATE reminders SET start_at=0')
        assert client.get('/devices/me/reminders', headers=auth).json()['reminders'] == []
        client.put('/devices/me/courses', headers=auth, json={'courses': []})
        with sqlite3.connect(path) as c:
            assert c.execute('SELECT count(*) FROM reminders').fetchone()[0] == 0
        assert client.delete('/devices/me/reminders', headers=auth).status_code == 200
