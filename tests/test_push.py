import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.push import JPushProvider, PushFailure


class FakeProvider:
    app_key = 'a' * 24

    def __init__(self, failures=0):
        self.sent = []
        self.failures = failures

    async def send(self, registration_id, reminder, delivery_id, now):
        if self.failures:
            self.failures -= 1
            raise PushFailure('network_error')
        self.sent.append((registration_id, reminder, delivery_id))
        return 'mock-message-id'


ADMIN = {'Authorization': 'Bearer ' + 'a' * 32}


def registration(rid='rid1'):
    return {'registration_id': rid, 'app_key': 'a' * 24,
            'manufacturer': 'Xiaomi', 'model': 'Test phone', 'android_version': '16'}


def pair(client, rid='rid1', code=None):
    code = code or client.post('/admin/pairings', headers=ADMIN).json()['code']
    return client.post('/devices/pair', json={'code': code, 'name': 'Phone', 'push': registration(rid)})


def plan(client, auth, now):
    reply = client.put('/devices/me/courses', headers=auth, json={
        'lead_minutes': 1, 'courses': [{'id': 'c1', 'course_name': 'Math', 'room': 'A101', 'start_at': now + 120}]})
    assert reply.status_code == 200


def test_dispatch_due_isolated_deduplicated_and_persistent(tmp_path):
    provider = FakeProvider()
    path = str(tmp_path / 'db')
    now = int(time.time())
    app = create_app(path, 'a' * 32, provider, start_dispatcher=False)
    with TestClient(app) as client:
        first, second = pair(client).json(), pair(client, 'rid2').json()
        auth = {'Authorization': 'Bearer ' + first['token']}
        plan(client, auth, now)
        asyncio.run(app.state.dispatcher.tick(now + 59))
        assert provider.sent == []
        asyncio.run(app.state.dispatcher.tick(now + 60))
        asyncio.run(app.state.dispatcher.tick(now + 61))
        assert [item[0] for item in provider.sent] == ['rid1']
        history = client.get('/admin/push-deliveries', headers=ADMIN).json()
        assert history[0]['state'] == 'accepted'
        assert client.get('/admin/push-deliveries').status_code == 401
        assert client.get('/devices/me/reminders', headers={'Authorization': 'Bearer ' + second['token']}).json()['reminders'] == []
    restarted = create_app(path, 'a' * 32, provider, start_dispatcher=False)
    with TestClient(restarted):
        asyncio.run(restarted.state.dispatcher.tick(now + 62))
    assert len(provider.sent) == 1


def test_route_uniqueness_rotation_and_pair_rollback(tmp_path):
    provider = FakeProvider()
    app = create_app(str(tmp_path / 'db'), 'a' * 32, provider, start_dispatcher=False)
    with TestClient(app) as client:
        first = pair(client).json()
        code = client.post('/admin/pairings', headers=ADMIN).json()['code']
        assert pair(client, code=code).status_code == 409
        assert pair(client, 'rid2', code=code).status_code == 200
        auth = {'Authorization': 'Bearer ' + first['token']}
        assert client.put('/devices/me/push', headers=auth, json=registration('rid3')).status_code == 200
        bad = registration('rid3') | {'app_key': 'b' * 24}
        assert client.put('/devices/me/push', headers=auth, json=bad).status_code == 409
        now = int(time.time())
        plan(client, auth, now)
        asyncio.run(app.state.dispatcher.tick(now + 60))
        assert provider.sent[0][0] == 'rid3'
        assert client.delete('/devices/me', headers=auth).status_code == 200
        assert client.put('/devices/me/push', headers=auth, json=registration('rid4')).status_code == 401


def test_retry_deadline_and_revoke(tmp_path):
    provider = FakeProvider(failures=1)
    app = create_app(str(tmp_path / 'db'), 'a' * 32, provider, start_dispatcher=False)
    with TestClient(app) as client:
        first = pair(client).json()
        auth = {'Authorization': 'Bearer ' + first['token']}
        now = int(time.time())
        plan(client, auth, now)
        asyncio.run(app.state.dispatcher.tick(now + 60))
        asyncio.run(app.state.dispatcher.tick(now + 61))
        assert provider.sent == []
        asyncio.run(app.state.dispatcher.tick(now + 65))
        assert len(provider.sent) == 1
        assert client.get('/admin/push-deliveries', headers=ADMIN).json()[0]['attempts'] == 2
        client.post('/admin/devices/' + first['device_id'] + '/test-reminder', headers=ADMIN)
        client.delete('/admin/devices/' + first['device_id'], headers=ADMIN)
        asyncio.run(app.state.dispatcher.tick(now + 90))
        assert len(provider.sent) == 1
        second = pair(client, 'rid2').json()
        plan(client, {'Authorization': 'Bearer ' + second['token']}, now)
        asyncio.run(app.state.dispatcher.tick(now + 121))
        assert len(provider.sent) == 1


def test_provider_payload_and_error_classification():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'msg_id': '1234'})

    provider = JPushProvider('a' * 24, 'server-only-secret', httpx.MockTransport(handler))
    reminder = {'course_name': 'Math', 'room': 'A101', 'start_at': 1800000000}
    assert asyncio.run(provider.send('rid1', reminder, 'delivery1', 1799999900)) == '1234'
    import json
    payload = json.loads(requests[0].content)
    assert payload['audience'] == {'registration_id': ['rid1']}
    assert payload['options']['time_to_live'] == 100
    assert payload['notification']['android']['extras']['delivery_id'] == 'delivery1'
    assert 'server-only-secret' not in requests[0].content.decode()
    for status, retryable in [(400, False), (401, False), (429, True), (503, True)]:
        p = JPushProvider('a' * 24, 'secret', httpx.MockTransport(lambda request: httpx.Response(status)))
        with pytest.raises(PushFailure) as exc:
            asyncio.run(p.send('rid1', reminder, 'id', 1799999900))
        assert exc.value.retryable is retryable


def test_lifespan_dispatches_without_phone_polling(tmp_path):
    import threading
    sent = threading.Event()

    class Provider(FakeProvider):
        async def send(self, *args):
            result = await super().send(*args)
            sent.set()
            return result

    app = create_app(str(tmp_path / 'db'), 'a' * 32, Provider())
    with TestClient(app) as client:
        binding = pair(client).json()
        auth = {'Authorization': 'Bearer ' + binding['token']}
        reply = client.put('/devices/me/courses', headers=auth, json={
            'lead_minutes': 1, 'courses': [{'id': 'live', 'course_name': 'Live test', 'start_at': int(time.time()) + 30}]})
        assert reply.status_code == 200
        assert sent.wait(10)
