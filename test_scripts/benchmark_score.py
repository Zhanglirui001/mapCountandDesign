"""Score block-diagram table predictions against ground truth."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class RowMismatch:
    node: str
    ground_truth: int
    predicted: int


@dataclass(frozen=True)
class ImageScore:
    stem: str
    strategy: str
    success: bool
    error: str = ""
    project_type_ok: bool = False
    ground_truth_type: str = ""
    predicted_type: str = ""
    recall: float = 0.0
    precision: float = 0.0
    f1: float = 0.0
    string_accuracy: float = 0.0
    exact_match: bool = False
    ground_truth_rows: int = 0
    predicted_rows: int = 0
    matched_rows: int = 0
    missing_nodes: tuple[str, ...] = ()
    extra_nodes: tuple[str, ...] = ()
    string_mismatches: tuple[RowMismatch, ...] = ()


@dataclass(frozen=True)
class StrategySummary:
    strategy: str
    images: int = 0
    successes: int = 0
    project_type_accuracy: float = 0.0
    mean_recall: float = 0.0
    mean_precision: float = 0.0
    mean_f1: float = 0.0
    mean_string_accuracy: float = 0.0
    exact_match_rate: float = 0.0
    per_image: tuple[ImageScore, ...] = ()


def load_ground_truth(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        key: value
        for key, value in payload.items()
        if not str(key).startswith("_")
    }


def normalize_project_type(project_type: str) -> str:
    """Map recognizer labels onto ground-truth project_type values."""

    value = project_type.strip().upper()
    if value in {"IBEX", "CBX"}:
        return "CBX"
    if value in {"LYNX", "LBD"}:
        return "LYNX"
    return value


def rows_to_map(string_info: Any) -> dict[str, int]:
    if not isinstance(string_info, list):
        return {}
    rows: dict[str, int] = {}
    for item in string_info:
        if not isinstance(item, Mapping):
            continue
        node = str(item.get("node", "")).strip()
        if not node:
            continue
        rows[node] = int(item["string_number"])
    return rows


def score_prediction(
    *,
    stem: str,
    strategy: str,
    ground_truth: Mapping[str, Any],
    prediction: Mapping[str, Any] | None,
    error: str = "",
) -> ImageScore:
    gt_type = normalize_project_type(str(ground_truth.get("project_type", "")))
    gt_rows = rows_to_map(ground_truth.get("string_info"))
    if prediction is None:
        return ImageScore(
            stem=stem,
            strategy=strategy,
            success=False,
            error=error or "missing prediction",
            ground_truth_type=gt_type,
            ground_truth_rows=len(gt_rows),
        )

    pred_type = normalize_project_type(str(prediction.get("project_type", "")))
    pred_rows = rows_to_map(prediction.get("string_info"))
    common = set(gt_rows) & set(pred_rows)
    missing = sorted(set(gt_rows) - set(pred_rows))
    extra = sorted(set(pred_rows) - set(gt_rows))
    mismatches = tuple(
        RowMismatch(node=node, ground_truth=gt_rows[node], predicted=pred_rows[node])
        for node in sorted(common)
        if gt_rows[node] != pred_rows[node]
    )

    recall = len(common) / len(gt_rows) if gt_rows else 1.0
    precision = len(common) / len(pred_rows) if pred_rows else 0.0
    f1 = (
        2 * recall * precision / (recall + precision)
        if recall + precision
        else 0.0
    )
    string_accuracy = (
        sum(1 for node in common if gt_rows[node] == pred_rows[node]) / len(common)
        if common
        else 0.0
    )
    project_type_ok = gt_type == pred_type
    exact_match = project_type_ok and gt_rows == pred_rows

    return ImageScore(
        stem=stem,
        strategy=strategy,
        success=exact_match,
        project_type_ok=project_type_ok,
        ground_truth_type=gt_type,
        predicted_type=pred_type,
        recall=recall,
        precision=precision,
        f1=f1,
        string_accuracy=string_accuracy,
        exact_match=exact_match,
        ground_truth_rows=len(gt_rows),
        predicted_rows=len(pred_rows),
        matched_rows=len(common),
        missing_nodes=tuple(missing),
        extra_nodes=tuple(extra),
        string_mismatches=mismatches,
    )


def summarize_strategy(scores: list[ImageScore]) -> StrategySummary:
    if not scores:
        return StrategySummary(strategy="", per_image=())
    strategy = scores[0].strategy
    scored = [score for score in scores if not score.error]
    count = len(scores)
    return StrategySummary(
        strategy=strategy,
        images=count,
        successes=sum(1 for score in scores if score.success),
        project_type_accuracy=(
            sum(1 for score in scored if score.project_type_ok) / count
        ),
        mean_recall=sum(score.recall for score in scored) / count,
        mean_precision=sum(score.precision for score in scored) / count,
        mean_f1=sum(score.f1 for score in scored) / count,
        mean_string_accuracy=(
            sum(score.string_accuracy for score in scored) / count
        ),
        exact_match_rate=sum(1 for score in scores if score.exact_match) / count,
        per_image=tuple(scores),
    )


def format_summary_markdown(
    summaries: Sequence[StrategySummary],
) -> str:
    lines = [
        "# Block Diagram OCR Benchmark",
        "",
        "## Overall",
        "",
        "| Strategy | Images | Success | project_type | Recall | Precision | F1 | String Acc | Exact |",
        "|----------|--------|---------|--------------|--------|-----------|----|-----------:|------:|",
    ]
    for summary in summaries:
        lines.append(
            f"| {summary.strategy} | {summary.images} | {summary.successes} | "
            f"{summary.project_type_accuracy:.1%} | {summary.mean_recall:.1%} | "
            f"{summary.mean_precision:.1%} | {summary.mean_f1:.1%} | "
            f"{summary.mean_string_accuracy:.1%} | {summary.exact_match_rate:.1%} |"
        )

    for summary in summaries:
        lines.extend(["", f"## {summary.strategy}", ""])
        lines.append(
            "| Image | OK | Type | GT→Pred Type | F1 | String Acc | Exact | GT rows | Pred rows |"
        )
        lines.append("|-------|:--:|------|--------------|---:|----------:|------:|--------:|----------:|")
        for score in summary.per_image:
            type_cell = (
                f"{score.ground_truth_type}→{score.predicted_type}"
                if not score.error
                else score.error
            )
            lines.append(
                f"| {score.stem} | {'Y' if score.success else 'N'} | "
                f"{'Y' if score.project_type_ok else 'N'} | {type_cell} | "
                f"{score.f1:.1%} | {score.string_accuracy:.1%} | "
                f"{'Y' if score.exact_match else 'N'} | {score.ground_truth_rows} | "
                f"{score.predicted_rows} |"
            )
            if score.missing_nodes or score.extra_nodes or score.string_mismatches:
                if score.missing_nodes:
                    lines.append(
                        f"| | | | missing: {', '.join(score.missing_nodes[:5])}"
                        f"{'…' if len(score.missing_nodes) > 5 else ''} | | | | | |"
                    )
                if score.extra_nodes:
                    lines.append(
                        f"| | | | extra: {', '.join(score.extra_nodes[:5])}"
                        f"{'…' if len(score.extra_nodes) > 5 else ''} | | | | | |"
                    )
                if score.string_mismatches:
                    sample = score.string_mismatches[:3]
                    detail = "; ".join(
                        f"{item.node} {item.ground_truth}->{item.predicted}"
                        for item in sample
                    )
                    suffix = "…" if len(score.string_mismatches) > 3 else ""
                    lines.append(
                        f"| | | | string mismatches: {detail}{suffix} | | | | | |"
                    )
    return "\n".join(lines) + "\n"
