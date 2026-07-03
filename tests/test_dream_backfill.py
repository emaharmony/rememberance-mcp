"""Phase 3.5 — dream-cycle embedding backfill (`_phase_embed_stale`, §5.5).

Re-embeds memories with a missing or wrong-model vector and reports a TRUE
count. Offline via the hash backend (current model = "hash-256").
"""

import sqlite3
import tempfile
from pathlib import Path

from remembrance_mcp.dream.cycle import DreamCycle
from remembrance_mcp.embed.embed import HashEmbedBackend
from remembrance_mcp.store.edges import EntityStore
from remembrance_mcp.store.memory import MemoryStoreV2
from remembrance_mcp.store.store import MemoryStore


def _setup(tmp):
    db = Path(tmp) / "m.db"
    store = MemoryStore(db_path=db)
    mv2 = MemoryStoreV2(v1_store=store)
    es = EntityStore(db_path=db)
    dream = DreamCycle(entity_store=es, memory_v2=mv2)  # embed_chain lazy → hash-256
    return db, store, dream


def _models(db):
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        return {
            r["id"]: (r["embedding_model"], r["embedding"] is not None)
            for r in conn.execute("SELECT id, embedding_model, embedding FROM memories").fetchall()
        }


def _seed(store):
    # Two rows with NO embedding (embedding_model defaults to '').
    n1 = store.store("alpha content one", "s", "misc", "active", [])
    n2 = store.store("alpha content two", "s", "misc", "active", [])
    # One row embedded by a DIFFERENT (stale) model.
    w = store.store(
        "beta wrong-model row",
        "s",
        "misc",
        "active",
        [],
        embedding=HashEmbedBackend().embed("beta wrong-model row"),
        embedding_dim=256,
        embedding_model="old-model-v1",
    )
    # One row already at the current model — must be left alone.
    ok = store.store(
        "gamma already current",
        "s",
        "misc",
        "active",
        [],
        embedding=HashEmbedBackend().embed("gamma already current"),
        embedding_dim=256,
        embedding_model="hash-256",
    )
    return n1, n2, w, ok


def test_backfill_embeds_null_and_wrong_model_rows():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, dream = _setup(tmp)
        n1, n2, w, ok = _seed(store)

        result = dream._phase_embed_stale()
        assert result["status"] == "ok"
        assert result["model"] == "hash-256"
        # 2 null + 1 wrong-model = 3 refreshed; the already-current row is skipped.
        assert result["stale_found"] == 3
        assert result["embeddings_refreshed"] == 3

        models = _models(db)
        for mid in (n1, n2, w, ok):
            model, has_vec = models[mid]
            assert model == "hash-256"
            assert has_vec


def test_backfill_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, dream = _setup(tmp)
        _seed(store)
        first = dream._phase_embed_stale()
        assert first["embeddings_refreshed"] == 3
        # Second run: everything is now current-model → nothing to do.
        second = dream._phase_embed_stale()
        assert second["stale_found"] == 0
        assert second["embeddings_refreshed"] == 0


def test_backfill_dry_run_reports_without_writing():
    with tempfile.TemporaryDirectory() as tmp:
        db, store, dream = _setup(tmp)
        _seed(store)
        before = _models(db)

        result = dream._phase_embed_stale(dry_run=True)
        assert result["embeddings_refreshed"] == 0
        assert result["stale_found"] == 3

        # Nothing changed on disk.
        assert _models(db) == before


def test_backfill_via_run_phase_selection():
    # The phase is reachable through the public dream run() with phase selection.
    with tempfile.TemporaryDirectory() as tmp:
        db, store, dream = _setup(tmp)
        _seed(store)
        report = dream.run(phases=["embed_stale"])
        # run() returns {"phases": [{"phase", "status", "details"}, ...]}.
        embed_phase = next(p for p in report["phases"] if p["phase"] == "embed_stale")
        assert embed_phase["status"] == "ok"
        assert embed_phase["details"]["embeddings_refreshed"] == 3
