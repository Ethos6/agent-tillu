"""In-process event bus feeding the browser over Server-Sent Events."""
from __future__ import annotations

import asyncio
import json
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def emit(self, kind: str, data: Any) -> None:
        payload = json.dumps({"type": kind, "data": data}, default=str)
        for q in list(self._subs):
            try:
                if self.loop and self.loop.is_running():
                    try:
                        running = asyncio.get_running_loop()
                    except RuntimeError:
                        running = None
                    if running is self.loop:
                        q.put_nowait(payload)
                    else:
                        self.loop.call_soon_threadsafe(q.put_nowait, payload)
                else:
                    q.put_nowait(payload)
            except asyncio.QueueFull:
                pass
