# Building the desktop app

Agent Tillu is a local web app, so turning it into a desktop app is a thin wrapper: the same server in a
background thread, shown in the operating system's own web view through [pywebview](https://pywebview.flowrl.com/).
No Electron, no Node, one binary.

```bash
pip install -e ".[desktop]"          # adds pywebview + pyinstaller
python -m tillu.desktop              # run it as a window (development)
python desktop/build.py              # build dist/Agent Tillu.app | .exe | agent-tillu
```

| Platform | Web view | Notes |
|---|---|---|
| macOS | WebKit (built in) | `dist/Agent Tillu.app`. Sign and notarize with your Apple ID for distribution. |
| Windows | WebView2 (built into Windows 10/11) | `dist/Agent Tillu.exe`. |
| Linux | WebKitGTK | `sudo apt install gir1.2-webkit2-4.1 python3-gi` first. `dist/agent-tillu`. |

The GitHub Actions workflow `.github/workflows/desktop.yml` builds all three on every tag (`v*`) and
attaches them to the release.

## How it's put together

- `tillu/desktop.py` starts uvicorn in a thread on a free localhost port, waits until it answers, then opens
  one pywebview window at `http://127.0.0.1:<port>/?token=…`. Everything else (API, firewall, routines) is
  unchanged from the CLI.
- `desktop/entry.py` is the PyInstaller entry. `--server` runs headless instead of opening a window.
- `desktop/build.py` calls PyInstaller with the web assets and uvicorn's lazily-imported modules included.

## Making it your own

Fork, change `APP_NAME` in `tillu/desktop.py`, replace `docs/logo-256.png`, and rebuild. To add native
menus, a tray icon or notifications, pywebview exposes the window object (`webview.create_window(...)`) and
a JS bridge (`window.pywebview.api`) you can extend in `tillu/desktop.py`.

Prefer Tauri or Electron? Point them at the same local server: run `agent-tillu --server --port 7860` as a
sidecar and load `http://127.0.0.1:7860/?token=<token from ~/.tillu/.token>`.
