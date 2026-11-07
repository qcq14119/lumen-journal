"""Embeddings of memory items (never of original turns), kept on disk.

Table ``mem_vectors`` holds one row per memory item and connection:
    item    memory id (e12, pf3, p2, pl1)
    kind    event | profile | person | plan
    digest  hash of the exact text that was embedded
    model   connection identity (address + model + dimensions)
    vector  float32 little-endian bytes

Only rows matching the current text and connection are used. Nothing is kept in
process memory between requests, so a small server only pays while searching.
"""

from __future__ import annotations

import array
import asyncio
import hashlib
import math

from . import core

BATCH = 32  # texts per embedding request
# People are always given to the chat model in full, so they are not searched.
KINDS = ("event", "profile", "plan")
KIND_NAMES = {"event": "剧情", "profile": "生活小档案", "plan": "约定"}
TEXT_VERSION = "v2"  # part of the digest: changing the text format re-embeds


def item_texts(memory) -> dict[str, tuple[str, str]]:
    """{item id: (kind, text to embed)} for what may need finding by meaning.

    Owner names and participant lists are left out: naming a person would
    otherwise make every entry about them look related.
    """
    memory = core.normalized(memory)
    items = {}
    for event in memory["events"]:
        text = " ".join(x for x in (event.get("title", ""), event.get("summary", ""), event.get("time", "")) if x)
        items[event["id"]] = ("event", text)
    for profile in memory["profiles"]:
        text = core.PROFILE_KINDS.get(profile.get("kind"), "背景") + "：" + profile.get("text", "")
        if profile.get("until"):
            text += f"（到{profile['until']}）"
        items[profile["id"]] = ("profile", text)
    for plan in memory["plans"]:
        if plan["status"] in core.OPEN:  # finished plans are never brought up in chat
            text = "约定：" + plan.get("summary", "") + (f"（{plan['time']}）" if plan.get("time") else "")
            items[plan["id"]] = ("plan", text)
    return {k: (kind, text.strip()) for k, (kind, text) in items.items() if text.strip()}


def digest(text: str) -> str:
    return hashlib.sha256((TEXT_VERSION + "\n" + text).encode()).hexdigest()


def pack(vector) -> bytes:
    return array.array("f", vector).tobytes()


def unpack(blob: bytes) -> array.array:
    values = array.array("f")
    values.frombytes(blob)
    return values


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class VectorIndex:
    def __init__(self, store):
        self.store = store
        self.running: dict[tuple[str, str], asyncio.Task] = {}
        with store.transaction():
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS mem_vectors("
                "scope TEXT NOT NULL, cid TEXT NOT NULL, item TEXT NOT NULL,"
                "kind TEXT NOT NULL, digest TEXT NOT NULL, model TEXT NOT NULL,"
                "vector BLOB NOT NULL, created INTEGER NOT NULL,"
                "PRIMARY KEY(scope,cid,item,model))"
            )

    # ------------------------------------------------------------ status --

    def status(self, scope, cid, model_id):
        """Counts per kind: expected items and how many have a current vector."""
        wanted = item_texts(self.store.memory(scope, cid))
        with self.store.lock:
            rows = {
                r["item"]: r["digest"]
                for r in self.store.db.execute(
                    "SELECT item,digest FROM mem_vectors WHERE scope=? AND cid=? AND model=?",
                    (scope, cid, model_id or ""),
                )
            }
            other = self.store.db.execute(
                "SELECT count(*) FROM mem_vectors WHERE scope=? AND cid=? AND model<>?",
                (scope, cid, model_id or ""),
            ).fetchone()[0]
        counts = {k: {"total": 0, "done": 0} for k in KINDS}
        for item, (kind, text) in wanted.items():
            counts[kind]["total"] += 1
            if rows.get(item) == digest(text):
                counts[kind]["done"] += 1
        total = sum(c["total"] for c in counts.values())
        done = sum(c["done"] for c in counts.values())
        return {
            "counts": counts,
            "total": total,
            "done": done,
            "missing": total - done,
            "stale": len(set(rows) - set(wanted)),
            "other_model": other,
            "running": (scope, cid) in self.running,
        }

    # -------------------------------------------------------------- sync --

    async def sync(self, scope, cid, model, *, rebuild=False):
        """Embed missing or changed items and drop removed ones. Returns counts.

        Raises the model error after keeping every batch that already succeeded,
        so a later "补齐" continues from there.
        """
        if rebuild:
            self.clear(scope, cid)
        wanted = item_texts(self.store.memory(scope, cid))
        with self.store.lock:
            have = {
                r["item"]: r["digest"]
                for r in self.store.db.execute(
                    "SELECT item,digest FROM mem_vectors WHERE scope=? AND cid=? AND model=?",
                    (scope, cid, model.identity),
                )
            }
        todo = [(i, k, t) for i, (k, t) in wanted.items() if have.get(i) != digest(t)]
        removed = [i for i in have if i not in wanted]
        with self.store.transaction():
            for item in removed:
                self.store.db.execute(
                    "DELETE FROM mem_vectors WHERE scope=? AND cid=? AND item=?", (scope, cid, item)
                )
            # A different connection's vectors are useless here; free the space.
            self.store.db.execute(
                "DELETE FROM mem_vectors WHERE scope=? AND cid=? AND model<>?", (scope, cid, model.identity)
            )
        added = 0
        for start in range(0, len(todo), BATCH):
            chunk = todo[start : start + BATCH]
            vectors = await model.get_embeddings([t for _, _, t in chunk])
            with self.store.transaction():
                for (item, kind, text), vector in zip(chunk, vectors):
                    self.store.db.execute(
                        "INSERT OR REPLACE INTO mem_vectors VALUES(?,?,?,?,?,?,?,strftime('%s','now'))",
                        (scope, cid, item, kind, digest(text), model.identity, pack(vector)),
                    )
            added += len(chunk)
        return {"added": added, "removed": len(removed), "total": len(wanted)}

    def clear(self, scope, cid):
        with self.store.transaction():
            self.store.db.execute("DELETE FROM mem_vectors WHERE scope=? AND cid=?", (scope, cid))

    def start(self, scope, cid, model, *, rebuild=False, on_done=None):
        """Run sync in the background once per chat. Returns False if one is running."""
        key = (scope, cid)
        if key in self.running:
            return False

        async def run():
            try:
                result = await self.sync(scope, cid, model, rebuild=rebuild)
                if on_done:
                    on_done(result, None)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if on_done:
                    on_done(None, error)

        task = asyncio.create_task(run())
        self.running[key] = task
        task.add_done_callback(lambda _: self.running.pop(key, None))
        return True

    async def close(self):
        tasks = list(self.running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.running.clear()

    # ------------------------------------------------------------ search --

    def score(self, scope, cid, model_id, query_vectors, memory, *, only=None):
        """([(similarity, item id)] best first, number of items without a current vector).

        Several query vectors may be given; each item keeps its best similarity.
        """
        if query_vectors and not isinstance(query_vectors[0], (list, tuple, array.array)):
            query_vectors = [query_vectors]
        current = {i: digest(t) for i, (_, t) in item_texts(memory).items()}
        if only is not None:
            only = set(only)
            current = {i: value for i, value in current.items() if i in only}
        results = []
        with self.store.lock:
            rows = self.store.db.execute(
                "SELECT item,digest,vector FROM mem_vectors WHERE scope=? AND cid=? AND model=?",
                (scope, cid, model_id),
            ).fetchall()
        fresh = 0
        for row in rows:
            if only is not None and row["item"] not in only:
                continue
            if current.get(row["item"]) != row["digest"]:
                continue  # text changed since it was embedded
            fresh += 1
            stored = unpack(row["vector"])
            results.append((max(cosine(q, stored) for q in query_vectors), row["item"]))
        results.sort(reverse=True)
        return results, len(current) - fresh
