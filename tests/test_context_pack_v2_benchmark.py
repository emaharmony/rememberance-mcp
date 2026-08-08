from __future__ import annotations

import hashlib
import json
from pathlib import Path


DATA = Path(__file__).parent / "data" / "context_pack_v2_benchmark.json"


def _evaluate(payload):
    permitted = [item for item in payload["results"] if item["project_id"] == "recall"]
    delivered = [
        item
        for item in permitted
        if item["disposition"] in {"inline", "summary", "reference"}
    ]
    ranked = sorted(delivered, key=lambda item: (item["rank"], item["id"]))
    top = ranked[:3]
    relevant_total = sum(bool(item["relevant"]) for item in permitted)
    relevant_top = sum(bool(item["relevant"]) for item in top)
    first_relevant = next(
        (position for position, item in enumerate(top, start=1) if item["relevant"]),
        0,
    )
    delivered_tokens = (
        payload["mandatory_tokens"]
        + payload["continuity_tokens"]
        + sum(item["delivered_tokens"] for item in delivered)
    )
    return {
        "mandatory_retention": sum(item["retained"] for item in payload["mandatory"])
        / len(payload["mandatory"]),
        "precision_at_3": relevant_top / 3,
        "recall_at_3": relevant_top / relevant_total,
        "mrr": 1 / first_relevant if first_relevant else 0,
        "useful_context_selection_rate": relevant_top / len(top),
        "tokens_delivered": delivered_tokens,
        "tokens_omitted": sum(item["omitted_tokens"] for item in permitted),
        "reference_expansion_rate": sum(
            item["disposition"] == "reference" for item in delivered
        )
        / len(delivered),
        "scope_leakage": sum(item["project_id"] != "recall" for item in permitted),
    }


def test_context_pack_v2_benchmark_is_deterministic_and_scope_safe():
    payload = json.loads(DATA.read_text(encoding="utf-8"))
    first = _evaluate(payload)
    second = _evaluate(json.loads(json.dumps(payload, sort_keys=True)))

    assert first == second
    assert (
        hashlib.sha256(json.dumps(first, sort_keys=True).encode("utf-8")).hexdigest()
        == hashlib.sha256(
            json.dumps(second, sort_keys=True).encode("utf-8")
        ).hexdigest()
    )
    assert first == {
        "mandatory_retention": 1.0,
        "precision_at_3": 1.0,
        "recall_at_3": 1.0,
        "mrr": 1.0,
        "useful_context_selection_rate": 1.0,
        "tokens_delivered": 490,
        "tokens_omitted": 700,
        "reference_expansion_rate": 1 / 3,
        "scope_leakage": 0,
    }
    assert first["tokens_delivered"] <= payload["token_budget"]
