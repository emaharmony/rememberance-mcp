from __future__ import annotations

import json
from pathlib import Path


DATASET = Path(__file__).parent / "data" / "retrieval_utility_benchmark.json"


def metrics(ranked, all_permitted, k):
    top = ranked[:k]
    relevant_total = sum(item["relevant"] for item in all_permitted)
    relevant_top = sum(item["relevant"] for item in top)
    reciprocal_rank = 0.0
    for rank, item in enumerate(ranked, start=1):
        if item["relevant"]:
            reciprocal_rank = 1.0 / rank
            break
    return {
        "precision_at_k": relevant_top / k,
        "recall_at_k": relevant_top / relevant_total,
        "mrr": reciprocal_rank,
        "useful_context_selection_rate": relevant_top / len(top),
        "cross_project_leakage": sum(
            item["project_id"] != "project-recall" for item in top
        ),
    }


def test_shadow_utility_benchmark_is_deterministic_and_non_regressing():
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    permitted = [
        item
        for item in dataset["memories"]
        if item["project_id"] == dataset["project_id"]
    ]
    current = sorted(permitted, key=lambda item: (-item["base_score"], item["id"]))
    shadow = sorted(
        permitted,
        key=lambda item: (
            -(item["base_score"] * 0.9 + item["utility"] * 0.1),
            item["id"],
        ),
    )
    current_metrics = metrics(current, permitted, dataset["k"])
    shadow_metrics = metrics(shadow, permitted, dataset["k"])
    assert current_metrics == {
        "precision_at_k": 1 / 3,
        "recall_at_k": 1 / 3,
        "mrr": 1.0,
        "useful_context_selection_rate": 1 / 3,
        "cross_project_leakage": 0,
    }
    assert shadow_metrics == {
        "precision_at_k": 2 / 3,
        "recall_at_k": 2 / 3,
        "mrr": 1.0,
        "useful_context_selection_rate": 2 / 3,
        "cross_project_leakage": 0,
    }
    assert shadow_metrics["precision_at_k"] >= current_metrics["precision_at_k"]
    assert shadow_metrics["recall_at_k"] >= current_metrics["recall_at_k"]
    assert shadow_metrics["cross_project_leakage"] == 0
