import pytest
from fastapi.testclient import TestClient

from server.cloud import from_env


@pytest.mark.parametrize('origin', ['', 'http://example.com', 'https://example.com/path',
                                    'https://user:pass@example.com', 'https://example.com?key=x',
                                    'https://example.com:0', 'https://example.com:70000',
                                    'https://example.com:invalid'])
def test_cloud_rejects_invalid_origins(monkeypatch, origin):
    monkeypatch.setenv('EAMS_PUBLIC_URL', origin)
    with pytest.raises(RuntimeError):
        from_env()


@pytest.mark.parametrize('port', ['0', '-1', '65536'])
def test_cloud_rejects_invalid_listen_ports(monkeypatch, port):
    monkeypatch.setenv('EAMS_PUBLIC_URL', 'https://example.com')
    monkeypatch.setenv('EAMS_PORT', port)
    with pytest.raises(RuntimeError):
        from_env()


def test_fixed_origin_pair_page_and_host_boundary(monkeypatch, tmp_path):
    monkeypatch.setenv('EAMS_PUBLIC_URL', 'https://example.com:8443')
    monkeypatch.setenv('EAMS_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('EAMS_PORT', '18080')
    with TestClient(from_env(), base_url='http://127.0.0.1:18080', client=('127.0.0.1', 1234)) as client:
        assert client.get('/health').status_code == 200
        page = client.get('/dev/pair')
        assert page.status_code == 200
        assert 'https://example.com:8443' in page.text
        assert client.get('/dev/pair', headers={'Host': 'example.com'}).status_code == 403
