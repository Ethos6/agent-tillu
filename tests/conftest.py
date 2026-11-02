import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tillu.config import load_config  # noqa: E402
from tillu.runtime import Runtime  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    return load_config(data_dir=str(tmp_path / "data"), default_model="stub", embed_model="none", token="test-token")


@pytest.fixture
async def rt(cfg):
    r = Runtime(cfg)
    r.db.set_setting("default_model", "stub")
    r.db.set_setting("embed_model", "none")
    r.apply_settings()
    r.bus.loop = asyncio.get_running_loop()
    yield r
    await r.shutdown()


async def settle(rt, timeout=5.0):
    """Wait until no run is queued or running."""
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        await asyncio.sleep(0.02)
        busy = rt.db.one("SELECT COUNT(*) AS n FROM runs WHERE status IN ('queued','running')")["n"]
        if not busy and not [t for t in rt.tasks if not t.done()]:
            return
    raise AssertionError("runs did not settle")
