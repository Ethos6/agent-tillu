"""python -m tillu  — start Agent Tillu on http://127.0.0.1:7860"""
from __future__ import annotations

import argparse
import webbrowser

import uvicorn

from .config import load_config
from .server import create_app


def main() -> None:
    p = argparse.ArgumentParser(prog="tillu", description="AI coworkers that run entirely on your computer.")
    p.add_argument("--data", help="data folder (default ~/.tillu)")
    p.add_argument("--port", type=int)
    p.add_argument("--host", help="listen address (default 127.0.0.1; anything else exposes Agent Tillu to your network)")
    p.add_argument("--model", help="default model, e.g. qwen3:8b, llama3.1:8b, or 'stub' to try it with no model")
    p.add_argument("--model-url", help="OpenAI-compatible base URL (default Ollama: http://127.0.0.1:11434/v1)")
    p.add_argument("--no-browser", action="store_true", help="don't open the web page automatically")
    a = p.parse_args()
    cfg = load_config(data_dir=a.data, port=a.port, host=a.host, default_model=a.model, model_base_url=a.model_url)
    app = create_app(cfg)
    if a.model:
        app.state.rt.db.set_setting("default_model", a.model)
    if a.model_url:
        app.state.rt.db.set_setting("model_base_url", a.model_url)
        app.state.rt.apply_settings()
    url = f"http://{'127.0.0.1' if cfg.host in ('0.0.0.0', '::') else cfg.host}:{cfg.port}/?token={cfg.token}"
    print("\n  👦 Agent Tillu is running — everything stays on this computer.")
    print(f"     Open: {url}")
    print(f"     Data: {cfg.data_dir}\n")
    if not a.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
