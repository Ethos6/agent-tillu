"""Build a standalone desktop app with PyInstaller.

    pip install -e ".[desktop]"
    python desktop/build.py

Output lands in dist/:  Agent Tillu.app (macOS), Agent Tillu.exe (Windows), agent-tillu (Linux).
The browser tools need Chromium at runtime; the app offers to install it on first use
(`python -m playwright install chromium`), so the binary itself stays small (~40 MB).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICON = ROOT / "docs" / "logo-256.png"


def main() -> None:
    name = "Agent Tillu" if sys.platform != "linux" else "agent-tillu"
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--name", name,
        "--windowed" if sys.platform != "linux" else "--onefile",
        "--add-data", f"{ROOT / 'tillu' / 'web'}{';' if sys.platform == 'win32' else ':'}tillu/web",
        "--hidden-import", "tillu.server", "--hidden-import", "uvicorn.logging", "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto", "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on", "--collect-submodules", "webview",
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
    ]
    if ICON.exists():
        icon = ICON
        if sys.platform == "darwin":
            icon = make_icns(ICON) or ICON
        elif sys.platform == "win32":
            icon = make_ico(ICON) or ICON
        args += ["--icon", str(icon)]
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", "app.tillu.agent"]
    args.append(str(ROOT / "desktop" / "entry.py"))
    print(" ".join(args))
    subprocess.check_call(args, cwd=ROOT)
    print("\nBuilt into", ROOT / "dist")


def make_ico(png: Path) -> Path | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    out = png.with_suffix(".ico")
    Image.open(png).save(out, sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256)])
    return out


def make_icns(png: Path) -> Path | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    out = png.with_suffix(".icns")
    Image.open(png).save(out)
    return out


if __name__ == "__main__":
    main()
