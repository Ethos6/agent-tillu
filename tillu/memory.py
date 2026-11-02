"""Coworker memory: SQLite FTS5 keyword search + optional local embeddings, with decay.

* Works with zero setup (FTS5 only). If an embedding model is available locally (e.g. Ollama's
  ``nomic-embed-text``), search becomes hybrid keyword + semantic.
* Memories that are used get stronger; untouched ones fade and drop out of the prompt (never deleted
  silently: they are still searchable and visible in the panel).
* Every change is written to a journal the agent's tools cannot touch, so you can see who changed what.
"""
from __future__ import annotations

import json
import math
import re
import time

from .db import DB, now

KINDS = ("rule", "preference", "fact", "note", "task")
HALF_LIFE_H = {"rule": 24 * 365, "preference": 24 * 120, "fact": 24 * 60, "note": 24 * 21, "task": 72}
DORMANT = 0.15


def strength(m: dict, t: float | None = None) -> float:
    if m.get("pinned"):
        return 1.0
    t = t or time.time()
    base = HALF_LIFE_H.get(m.get("kind", "fact"), 24 * 21)
    half = base * (1 + math.log2(1 + (m.get("access_count") or 0)))
    anchor = m.get("last_accessed_at") or m["created_at"]
    age_h = max(0.0, (t - anchor) / 3600)
    return 0.5 ** (age_h / half)


def _fts_query(q: str) -> str:
    words = [w for w in re.findall(r"[\w@.\-]+", q.lower()) if len(w) > 1][:12]
    return " OR ".join(f'"{w}"' for w in words)


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class Memory:
    def __init__(self, db: DB, llm=None, embed_model: str = ""):
        self.db = db
        self.llm = llm
        self.embed_model = embed_model

    async def _embed(self, text: str) -> list[float] | None:
        if not self.llm or not self.embed_model:
            return None
        vecs = await self.llm.embed(self.embed_model, [text])
        return vecs[0] if vecs else None

    def _journal(self, agent_id: str, action: str, content: str, by: str) -> None:
        self.db.insert("memory_journal", {"agent_id": agent_id, "action": action, "content": content, "by": by, "created_at": now()})

    async def add(self, agent_id: str, content: str, kind: str = "fact", by: str = "agent", pinned: bool = False) -> dict:
        content = " ".join(content.split())[:1000]
        kind = kind if kind in KINDS else "fact"
        dup = self.db.one("SELECT * FROM memories WHERE agent_id=? AND archived=0 AND lower(content)=lower(?)", (agent_id, content))
        if dup:
            self.db.update("memories", "id", dup["id"], {"last_accessed_at": now(), "access_count": dup["access_count"] + 1})
            return {**dup, "duplicate": True}
        emb = await self._embed(content)
        mid = self.db.insert("memories", {
            "agent_id": agent_id, "kind": kind, "content": content, "embedding": json.dumps(emb) if emb else None,
            "pinned": int(pinned or kind == "rule"), "source": by, "created_at": now(),
        })
        self._journal(agent_id, "add", content, by)
        return self.db.one("SELECT * FROM memories WHERE id=?", (mid,))

    def edit(self, agent_id: str, mem_id: int, content: str, by: str = "user") -> None:
        old = self.db.one("SELECT * FROM memories WHERE id=? AND agent_id=?", (mem_id, agent_id))
        if not old:
            raise KeyError(mem_id)
        self.db.update("memories", "id", mem_id, {"content": content, "embedding": None})
        self._journal(agent_id, "edit", f"{old['content']} → {content}", by)

    def remove(self, agent_id: str, mem_id: int, by: str = "user") -> dict | None:
        old = self.db.one("SELECT * FROM memories WHERE id=? AND agent_id=?", (mem_id, agent_id))
        if old:
            self.db.execute("DELETE FROM memories WHERE id=?", (mem_id,))
            self._journal(agent_id, "remove", old["content"], by)
        return old

    async def search(self, agent_id: str, query: str, k: int = 6, touch: bool = True) -> list[dict]:
        rows: dict[int, dict] = {}
        fq = _fts_query(query)
        if fq:
            for r in self.db.all(
                "SELECT m.*, bm25(memories_fts) AS rank FROM memories_fts JOIN memories m ON m.id = memories_fts.rowid "
                "WHERE memories_fts MATCH ? AND m.agent_id=? AND m.archived=0 ORDER BY rank LIMIT 30", (fq, agent_id)):
                r["kw"] = 1.0 / (1.0 + max(0.0, r["rank"] + 10))  # bm25 is negative: lower = better
                rows[r["id"]] = r
        qv = await self._embed(query)
        if qv:
            for r in self.db.all("SELECT * FROM memories WHERE agent_id=? AND archived=0 AND embedding IS NOT NULL", (agent_id,)):
                sim = _cos(qv, json.loads(r["embedding"]))
                if sim > 0.35:
                    rows.setdefault(r["id"], r)["sem"] = sim
        t = time.time()
        for r in rows.values():
            kw = r.get("kw", 0.0)
            kw = min(1.0, 0.5 + kw) if "kw" in r else 0.0
            r["score"] = (0.5 * kw + 0.5 * r.get("sem", 0.0) if qv else kw) * (0.5 + 0.5 * strength(r, t))
        best = sorted(rows.values(), key=lambda r: r["score"], reverse=True)[:k]
        if touch:
            for r in best:
                self.db.execute("UPDATE memories SET access_count=access_count+1, last_accessed_at=? WHERE id=?", (t, r["id"]))
        for r in best:
            r.pop("embedding", None)
        return best

    async def forget(self, agent_id: str, query: str, by: str = "agent") -> dict:
        hits = await self.search(agent_id, query, k=3, touch=False)
        exact = [h for h in hits if query.lower().strip() in h["content"].lower()]
        pick = exact if exact else hits
        if not pick:
            return {"removed": None, "reason": "nothing matched"}
        if len(pick) > 1 and (len(exact) != 1) and pick[0]["score"] - pick[1]["score"] < 0.05:
            return {"removed": None, "reason": "ambiguous", "candidates": [p["content"] for p in pick]}
        self.remove(agent_id, pick[0]["id"], by=by)
        return {"removed": pick[0]["content"]}

    def core(self, agent_id: str, max_chars: int = 2500) -> list[dict]:
        """What goes in every prompt: pinned rules first, then the strongest preferences/facts."""
        t = time.time()
        rows = self.db.all("SELECT * FROM memories WHERE agent_id=? AND archived=0", (agent_id,))
        rows = [r for r in rows if r["pinned"] or (r["kind"] in ("preference", "rule", "fact") and strength(r, t) >= DORMANT)]
        rows.sort(key=lambda r: (not r["pinned"], -strength(r, t)))
        out, used = [], 0
        for r in rows:
            if used + len(r["content"]) > max_chars:
                break
            out.append(r)
            used += len(r["content"])
        return out

    def list(self, agent_id: str) -> list[dict]:
        t = time.time()
        rows = self.db.all("SELECT id, kind, content, pinned, access_count, last_accessed_at, created_at, source, archived FROM memories WHERE agent_id=? ORDER BY pinned DESC, id DESC", (agent_id,))
        for r in rows:
            r["strength"] = round(strength(r, t), 3)
        return rows

    def as_markdown(self, agent_id: str) -> str:
        lines = ["# Memory", ""]
        for kind in KINDS:
            items = [r for r in self.list(agent_id) if r["kind"] == kind]
            if items:
                lines += [f"## {kind.title()}s", *[f"- {r['content']}" for r in items], ""]
        return "\n".join(lines)
