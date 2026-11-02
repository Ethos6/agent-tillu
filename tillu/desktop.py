"""Desktop app: the same server, wrapped in a native window.

    python -m tillu.desktop            # run as a desktop app
    python desktop/build.py            # build a standalone .app / .exe / binary with PyInstaller

The window is pywebview (a thin wrapper over the OS's own web view: WebKit on macOS, WebView2 on
Windows, WebKitGTK on Linux), so the whole app is one small binary with no Electron and no Node.
The server still binds to 127.0.0.1 with a token, exactly like the CLI, so your normal browser can
open the same URL too.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import uvicorn

from .config import load_config
from .server import create_app

APP_NAME = "Agent Tillu"


def free_port(preferred: int) -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def start_server(cfg, app) -> threading.Thread:
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True, name="tillu-server")
    t.start()
    return t


def wait_ready(url: str, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except urllib.error.HTTPError:
            return True  # any HTTP answer (even 401 without the token) means the server is up
        except Exception:
            time.sleep(0.15)
    return False


def main() -> None:
    try:
        import webview  # pywebview
    except ImportError:
        print("The desktop window needs pywebview:  pip install pywebview", file=sys.stderr)
        sys.exit(1)
    cfg = load_config()
    cfg.port = free_port(cfg.port)
    app = create_app(cfg)
    start_server(cfg, app)
    base = f"http://127.0.0.1:{cfg.port}"
    if not wait_ready(f"{base}/", 20):
        print("Server did not start", file=sys.stderr)
        sys.exit(1)
    window = webview.create_window(APP_NAME, f"{base}/?token={cfg.token}", width=1280, height=840, min_size=(760, 560),
                                   text_select=True)
    # Links the page opens in a new tab (exports, screenshots) go to the system browser.
    webview.start(private_mode=False)
    del window


if __name__ == "__main__":
    main()
