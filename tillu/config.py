"""Runtime configuration. Everything defaults to local: nothing here points at a cloud service."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 7860
    # Any OpenAI-compatible endpoint. Ollama, LM Studio, llama.cpp server and vLLM all speak it.
    model_base_url: str = "http://127.0.0.1:11434/v1"
    model_api_key: str = "local"
    default_model: str = "qwen3:8b"
    embed_model: str = "nomic-embed-text"
    max_steps: int = 24
    turn_timeout_s: int = 900
    num_ctx: int = 16384  # context window asked of Ollama (its default is far too small for tools)
    context_chars: int = 36_000  # ~9k tokens of history; with prompt + tools fits a 16k local context
    token: str = field(default="")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tillu.db"

    @property
    def workspaces(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def shared_skills(self) -> Path:
        return self.data_dir / "skills"

    def ensure(self) -> "Config":
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces.mkdir(exist_ok=True)
        self.shared_skills.mkdir(exist_ok=True)
        token_file = self.data_dir / ".token"
        if not self.token:
            if token_file.exists():
                self.token = token_file.read_text().strip()
            else:
                self.token = secrets.token_urlsafe(24)
                token_file.write_text(self.token)
                try:
                    token_file.chmod(0o600)
                except OSError:
                    pass
        return self


def load_config(**overrides) -> Config:
    data_dir = Path(overrides.pop("data_dir", None) or _env("TILLU_DATA", str(Path.home() / ".tillu"))).expanduser()
    cfg = Config(
        data_dir=data_dir,
        host=_env("TILLU_HOST", "127.0.0.1"),
        port=int(_env("TILLU_PORT", "7860")),
        model_base_url=_env("TILLU_MODEL_URL", "http://127.0.0.1:11434/v1"),
        model_api_key=_env("TILLU_MODEL_KEY", "local"),
        default_model=_env("TILLU_MODEL", "qwen3:8b"),
        embed_model=_env("TILLU_EMBED_MODEL", "nomic-embed-text"),
        max_steps=int(_env("TILLU_MAX_STEPS", "24")),
        token=_env("TILLU_TOKEN", ""),
    )
    for k, v in overrides.items():
        if v is not None:
            setattr(cfg, k, v)
    return cfg.ensure()
