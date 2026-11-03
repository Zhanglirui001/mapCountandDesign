from __future__ import annotations

import json
from pathlib import Path

from benchmark_score import (
    load_ground_truth,
    normalize_project_type,
    score_prediction,
    summarize_strategy,
)


def test_normalize_project_type_maps_recognizer_labels() -> None:
    assert normalize_project_type("IBEX") == "CBX"
    assert normalize_project_type("CBX") == "CBX"
    assert normalize_project_type("LBD") == "LYNX"
    assert normalize_project_type("LYNX") == "LYNX"


def test_score_prediction_treats_ibex_as_cbx() -> None:
    ground_truth = {
        "project_type": "CBX",
        "string_info": [{"node": "CBX-01", "string_number": 12}],
    }
    prediction = {
        "project_type": "IBEX",
        "string_info": [{"node": "CBX-01", "string_number": 12}],
    }
    score = score_prediction(
        stem="sample",
        strategy="yolo_claude",
        ground_truth=ground_truth,
        prediction=prediction,
    )
    assert score.project_type_ok is True
    assert score.exact_match is True


def test_score_prediction_exact_match() -> None:
    ground_truth = {
        "project_type": "CBX",
        "string_info": [
            {"node": "CBX-01", "string_number": 12},
            {"node": "CBX-02", "string_number": 18},
        ],
    }
    prediction = json.loads(json.dumps(ground_truth))
    score = score_prediction(
        stem="sample",
        strategy="mineru_claude",
        ground_truth=ground_truth,
        prediction=prediction,
    )
    assert score.success is True
    assert score.exact_match is True
    assert score.project_type_ok is True
    assert score.f1 == 1.0
    assert score.string_accuracy == 1.0


def test_score_prediction_counts_project_type_and_string_errors() -> None:
    ground_truth = {
        "project_type": "LYNX",
        "string_info": [
            {"node": "LBD-01", "string_number": 18},
            {"node": "LBD-02", "string_number": 16},
        ],
    }
    prediction = {
        "project_type": "CBX",
        "string_info": [
            {"node": "LBD-01", "string_number": 18},
            {"node": "LBD-03", "string_number": 16},
        ],
    }
    score = score_prediction(
        stem="sample",
        strategy="mineru_claude",
        ground_truth=ground_truth,
        prediction=prediction,
    )
    assert score.success is False
    assert score.project_type_ok is False
    assert score.recall == 0.5
    assert score.precision == 0.5
    assert score.string_accuracy == 1.0
    assert score.missing_nodes == ("LBD-02",)
    assert score.extra_nodes == ("LBD-03",)


def test_load_ground_truth_strips_review_fields(tmp_path: Path) -> None:
    path = tmp_path / "sample.json"
    path.write_text(
        json.dumps(
            {
                "_confidence": "high",
                "project_type": "CBX",
                "string_info": [{"node": "CBX-01", "string_number": 1}],
            }
        ),
        encoding="utf-8",
    )
    payload = load_ground_truth(path)
    assert "_confidence" not in payload
    assert payload["project_type"] == "CBX"


def test_summarize_strategy_averages_metrics() -> None:
    scores = [
        score_prediction(
            stem="a",
            strategy="mineru_claude",
            ground_truth={"project_type": "CBX", "string_info": []},
            prediction={"project_type": "CBX", "string_info": []},
        ),
        score_prediction(
            stem="b",
            strategy="mineru_claude",
            ground_truth={"project_type": "LYNX", "string_info": []},
            prediction={"project_type": "CBX", "string_info": []},
        ),
    ]
    summary = summarize_strategy(scores)
    assert summary.images == 2
    assert summary.successes == 1
    assert summary.project_type_accuracy == 0.5
    assert summary.exact_match_rate == 0.5
