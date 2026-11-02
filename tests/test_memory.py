import time

import pytest

from tillu.db import DB
from tillu.memory import Memory, strength

pytestmark = pytest.mark.asyncio


class FakeEmbed:
    """Maps words to axes so 'cat' and 'kitten' are close."""
    AX = {"cat": 0, "kitten": 0, "feline": 0, "invoice": 1, "bill": 1, "payment": 1, "tea": 2}

    async def embed(self, model, texts):
        out = []
        for t in texts:
            v = [0.0, 0.0, 0.0, 0.01]
            for w in t.lower().split():
                if w.strip(".,") in self.AX:
                    v[self.AX[w.strip(".,")]] += 1
            out.append(v)
        return out


async def test_keyword_search_and_forget(tmp_path):
    m = Memory(DB(tmp_path / "m.db"))
    await m.add("a", "The client Acme pays invoices on net 30", "fact")
    await m.add("a", "Prefers tea over coffee", "preference")
    dup = await m.add("a", "prefers tea over coffee", "preference")
    assert dup.get("duplicate")
    hits = await m.search("a", "when does Acme pay")
    assert hits[0]["content"].startswith("The client Acme")
    assert (await m.forget("a", "tea"))["removed"] == "Prefers tea over coffee"
    assert len(m.list("a")) == 1
    journal = m.db.all("SELECT action FROM memory_journal")
    assert [j["action"] for j in journal] == ["add", "add", "remove"]


async def test_semantic_search(tmp_path):
    m = Memory(DB(tmp_path / "m.db"), FakeEmbed(), "fake")
    await m.add("a", "Owner has a cat named Miso", "fact")
    await m.add("a", "Send the bill on the first of the month", "rule")
    hits = await m.search("a", "kitten")  # no keyword overlap
    assert hits and "cat" in hits[0]["content"]
    hits = await m.search("a", "payment schedule")
    assert hits and "bill" in hits[0]["content"]


async def test_decay(tmp_path):
    m = Memory(DB(tmp_path / "m.db"))
    row = await m.add("a", "temporary task note", "task")
    old = {**row, "created_at": time.time() - 3600 * 24 * 30, "last_accessed_at": None}
    assert strength(old) < 0.15
    rule = await m.add("a", "never email on weekends", "rule")
    assert rule["pinned"] == 1 and strength(rule) == 1.0
    m.db.update("memories", "id", row["id"], {"created_at": old["created_at"]})
    core = [c["content"] for c in m.core("a")]
    assert "never email on weekends" in core and "temporary task note" not in core
