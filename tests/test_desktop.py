import pytest

from tillu.desktop import free_port, start_server, wait_ready
from tillu.server import create_app


def test_desktop_server_thread(cfg):
    cfg.port = free_port(7999)
    app = create_app(cfg)  # the desktop wrapper owns its own runtime on the server thread
    start_server(cfg, app)
    assert wait_ready(f"http://127.0.0.1:{cfg.port}/", 15)
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{cfg.port}/", headers={"Cookie": "tillu_token=test-token"})
    r = urllib.request.urlopen(req)
    assert r.status == 200 and b"Agent Tillu" in r.read()
