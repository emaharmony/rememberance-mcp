"""Phase 3.1 — embed on write in capture() (non-blocking).

capture() should embed the content (default: the offline hash backend) and
persist the vector + dim + model on the memory row. Crucially, an embedding
failure must NOT lose the memory: the row is still stored, with null embedding.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from remembrance_mcp.config import Settings
from remembrance_mcp.extract import StubExtractor
from remembrance_mcp.gate.backends import GateFallbackChain, HeuristicBackend
from remembrance_mcp.pipeline import MemoryPipeline


@pytest.fixture
def pipeline():
    """Full pipeline, temp DB, heuristic gate + stub extractor (offline, hash embed)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(DB_PATH=Path(tmpdir) / "memories.db", GATE_MODEL_PATH=None)
        pipe = MemoryPipeline(settings=settings)
        pipe.gate_chain = GateFallbackChain([HeuristicBackend()])
        pipe.extractor = StubExtractor()
        yield pipe


def _row(pipe, mem_id):
    with sqlite3.connect(str(pipe.store.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT embedding, embedding_dim, embedding_model FROM memories WHERE id = ?",
            (mem_id,),
        ).fetchone()


def test_capture_embeds_and_persists_vector(pipeline):
    result = pipeline.capture("We decided the event bus is the canonical transport.", source="test")
    assert result["decision"] != "SKIP"
    mem_id = result["id"]
    assert mem_id

    row = _row(pipeline, mem_id)
    # Default offline backend is the hash scaffold: 256-dim vector, model "hash-256".
    assert row["embedding"] is not None
    assert row["embedding_dim"] == 256
    assert row["embedding_model"] == "hash-256"
    # Byte length matches the recorded dimension (float32 = 4 bytes each).
    assert len(row["embedding"]) == row["embedding_dim"] * 4


def test_capture_is_nonblocking_on_embed_failure(pipeline):
    # Force the embedder to fail; the memory must still be stored, sans vector.
    class _Boom:
        def embed_text(self, text):
            raise RuntimeError("embedder down")

    pipeline.embed_chain = _Boom()

    result = pipeline.capture(
        "We decided the fallback path must never drop memories.", source="test"
    )
    assert result["decision"] != "SKIP"
    mem_id = result["id"]
    assert mem_id  # memory was NOT lost

    row = _row(pipeline, mem_id)
    assert row["embedding"] is None
    assert row["embedding_dim"] is None
    assert row["embedding_model"] == ""


def test_skip_does_not_store_or_embed(pipeline):
    # A trivial utterance is gated SKIP → no row, nothing to embed.
    result = pipeline.capture("ok", source="test")
    assert result["decision"] == "SKIP"
    assert result["id"] is None


def test_embedding_is_deterministic_across_captures(pipeline):
    # Same text → identical hash vector (useful sanity check for the write path).
    r1 = pipeline.capture("Canonical decision: alpha subsystem owns routing.", source="test")
    r2 = pipeline.capture("Canonical decision: alpha subsystem owns routing.", source="test")
    e1 = _row(pipeline, r1["id"])["embedding"]
    e2 = _row(pipeline, r2["id"])["embedding"]
    assert e1 == e2
