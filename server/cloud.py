"""Fixed HTTPS origin with a loopback-only pairing management page."""
import os
from urllib.parse import urlsplit

from server.dev import create_dev_app


def from_env():
    origin = os.environ.get('EAMS_PUBLIC_URL', '').rstrip('/')
    parts = urlsplit(origin)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.path or parts.query or parts.fragment):
        raise RuntimeError('EAMS_PUBLIC_URL must be a root HTTPS origin')
    try:
        if parts.port == 0:
            raise ValueError('Invalid HTTPS port')
    except ValueError as exc:
        raise RuntimeError('EAMS_PUBLIC_URL has an invalid port') from exc
    port = int(os.environ.get('EAMS_PORT', '18080'))
    if not 1 <= port <= 65535:
        raise RuntimeError('EAMS_PORT must be between 1 and 65535')
    app = create_dev_app(os.environ.get('EAMS_STATE_DIR', '.local-dev'), port=port, public=True)
    app.state.public_origin = origin
    return app
