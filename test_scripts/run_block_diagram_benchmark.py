"""Run block-diagram benchmark for mineru_claude, yolo_claude, and yolo_glm."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from count_and_design.application.ai_statistics.element_recognition.block_diagram_table import (
    STRATEGY_MINERU_CLAUDE,
    STRATEGY_YOLO_CLAUDE,
    STRATEGY_YOLO_GLM,
    BlockDiagramTableError,
    BlockDiagramTableRecognizer,
    BlockDiagramTableResult,
)
from count_and_design.application.config import BlockDiagramTableConfig
from benchmark_score import (
    StrategySummary,
    format_summary_markdown,
    load_ground_truth,
    score_prediction,
    summarize_strategy,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_IMAGE_DIR = Path(
    r"C:\Users\Voltage\Downloads\标注工作区\OCR识别区"
)
GROUND_TRUTH_DIR = ROOT_DIR / "benchmark/ground_truth"
RESULTS_DIR = ROOT_DIR / "benchmark/results"
SUPPORTED_STRATEGIES = (
    STRATEGY_MINERU_CLAUDE,
    STRATEGY_YOLO_CLAUDE,
    STRATEGY_YOLO_GLM,
)
_YOLO_VISION_STRATEGIES = frozenset({STRATEGY_YOLO_CLAUDE, STRATEGY_YOLO_GLM})


@dataclass(frozen=True)
class StrategySpec:
    strategy: str
    prediction_dir: Path

    def prediction_path(self, stem: str) -> Path:
        if self.strategy == STRATEGY_MINERU_CLAUDE:
            return self.prediction_dir / f"{stem}_block_table.json"
        return self.prediction_dir / f"{stem}.json"


STRATEGY_SPECS: dict[str, StrategySpec] = {
    STRATEGY_MINERU_CLAUDE: StrategySpec(
        STRATEGY_MINERU_CLAUDE,
        ROOT_DIR / "ocr_outputs/claude",
    ),
    STRATEGY_YOLO_CLAUDE: StrategySpec(
        STRATEGY_YOLO_CLAUDE,
        ROOT_DIR / "ocr_outputs/yolo_claude",
    ),
    STRATEGY_YOLO_GLM: StrategySpec(
        STRATEGY_YOLO_GLM,
        ROOT_DIR / "ocr_outputs/yolo_glm",
    ),
}


def _append_log(log_path: Path, line: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _load_prediction(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_prediction(result: BlockDiagramTableResult, output_path: Path) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if result.strategy in _YOLO_VISION_STRATEGIES:
        payload: dict[str, Any] = result.to_dict()
        payload["strategy"] = result.strategy
        payload["table_bbox"] = (
            {
                "x1": result.table_bbox.x1,
                "y1": result.table_bbox.y1,
                "x2": result.table_bbox.x2,
                "y2": result.table_bbox.y2,
            }
            if result.table_bbox is not None
            else None
        )
        payload["raw_output"] = dict(result.raw_output)
        payload["cropped_image_path"] = (
            str(result.cropped_image_path)
            if result.cropped_image_path is not None
            else None
        )
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return payload

    output_path.write_text(result.to_json(), encoding="utf-8")
    return json.loads(result.to_json())


def _build_recognizer(*, env_path: str, strategy: str, output_dir: Path) -> BlockDiagramTableRecognizer:
    config = replace(
        BlockDiagramTableConfig.from_env(env_path),
        strategy=strategy,
    )
    kwargs: dict[str, Any] = {}
    if strategy in _YOLO_VISION_STRATEGIES:
        crop_dir = output_dir / "crops"
        crop_dir.mkdir(parents=True, exist_ok=True)
        kwargs["temp_dir"] = crop_dir
    return BlockDiagramTableRecognizer(config=config, **kwargs)


def _run_prediction(
    *,
    image_path: Path,
    env_path: str,
    spec: StrategySpec,
) -> dict[str, Any]:
    recognizer = _build_recognizer(
        env_path=env_path,
        strategy=spec.strategy,
        output_dir=spec.prediction_dir,
    )
    result = recognizer.recognize(image_path)
    return _save_prediction(result, spec.prediction_path(image_path.stem))


def _resolve_image(stem: str, image_dir: Path) -> Path | None:
    candidate = image_dir / f"{stem}.png"
    return candidate if candidate.is_file() else None


def _summary_to_dict(summary: StrategySummary) -> dict[str, Any]:
    return {
        "strategy": summary.strategy,
        "images": summary.images,
        "successes": summary.successes,
        "project_type_accuracy": summary.project_type_accuracy,
        "mean_recall": summary.mean_recall,
        "mean_precision": summary.mean_precision,
        "mean_f1": summary.mean_f1,
        "mean_string_accuracy": summary.mean_string_accuracy,
        "exact_match_rate": summary.exact_match_rate,
        "per_image": [
            {
                "stem": score.stem,
                "success": score.success,
                "error": score.error,
                "project_type_ok": score.project_type_ok,
                "ground_truth_type": score.ground_truth_type,
                "predicted_type": score.predicted_type,
                "recall": score.recall,
                "precision": score.precision,
                "f1": score.f1,
                "string_accuracy": score.string_accuracy,
                "exact_match": score.exact_match,
                "ground_truth_rows": score.ground_truth_rows,
                "predicted_rows": score.predicted_rows,
                "matched_rows": score.matched_rows,
                "missing_nodes": list(score.missing_nodes),
                "extra_nodes": list(score.extra_nodes),
                "string_mismatches": [
                    {
                        "node": item.node,
                        "ground_truth": item.ground_truth,
                        "predicted": item.predicted,
                    }
                    for item in score.string_mismatches
                ],
            }
            for score in summary.per_image
        ],
    }


def _score_strategy(
    *,
    spec: StrategySpec,
    ground_truth_files: list[Path],
    image_dir: Path,
    env_path: str,
    run_all: bool,
    run_missing: bool,
    log_path: Path,
) -> StrategySummary:
    run_jobs = [
        gt_path.stem
        for gt_path in ground_truth_files
        if run_all or not spec.prediction_path(gt_path.stem).is_file()
    ] if run_all or run_missing else []
    total_jobs = len(run_jobs)
    scores = []

    for gt_path in ground_truth_files:
        stem = gt_path.stem
        ground_truth = load_ground_truth(gt_path)
        pred_path = spec.prediction_path(stem)
        prediction: dict[str, Any] | None = None
        error = ""

        should_run = run_all or (run_missing and not pred_path.is_file())
        if should_run:
            image_path = _resolve_image(stem, image_dir)
            if image_path is None:
                error = f"image not found: {image_dir / (stem + '.png')}"
                line = f"[skip] {spec.strategy} {stem}: {error}"
                print(line)
                _append_log(log_path, line)
            else:
                job_no = run_jobs.index(stem) + 1
                prefix = f"[{job_no}/{total_jobs}]"
                started = time.perf_counter()
                try:
                    prediction = _run_prediction(
                        image_path=image_path,
                        env_path=env_path,
                        spec=spec,
                    )
                    elapsed = time.perf_counter() - started
                    line = f"{prefix} {spec.strategy} {stem} ({elapsed:.1f}s)"
                    print(line)
                    _append_log(log_path, line)
                except BlockDiagramTableError as exc:
                    elapsed = time.perf_counter() - started
                    error = str(exc)
                    line = (
                        f"{prefix} FAIL {spec.strategy} {stem} "
                        f"({elapsed:.1f}s): {exc}"
                    )
                    print(line)
                    _append_log(log_path, line)
        else:
            try:
                prediction = _load_prediction(pred_path)
                if prediction is None:
                    error = f"missing prediction: {pred_path}"
            except json.JSONDecodeError as exc:
                error = f"invalid prediction JSON: {exc}"

        scores.append(
            score_prediction(
                stem=stem,
                strategy=spec.strategy,
                ground_truth=ground_truth,
                prediction=prediction,
                error=error,
            )
        )

    return summarize_strategy(scores)


def main() -> None:
    parser = argparse.ArgumentParser(description="Block diagram OCR benchmark")
    parser.add_argument(
        "--image-dir",
        type=Path,
        default=DEFAULT_IMAGE_DIR,
        help="Directory containing benchmark PNG files",
    )
    parser.add_argument(
        "--env",
        default=str(ROOT_DIR / ".env"),
        help="Env file for recognizer configuration",
    )
    parser.add_argument(
        "--strategies",
        nargs="+",
        choices=SUPPORTED_STRATEGIES,
        default=list(SUPPORTED_STRATEGIES),
        help="Recognition strategies to run or score",
    )
    parser.add_argument(
        "--run-missing",
        action="store_true",
        help="Run recognizer only for images without saved predictions",
    )
    parser.add_argument(
        "--run-all",
        action="store_true",
        help="Re-run recognizer for every benchmark image (full refresh)",
    )
    args = parser.parse_args()

    if not args.run_all and not args.run_missing:
        print(
            "Scoring existing predictions only. "
            "Use --run-all or --run-missing to refresh predictions."
        )

    ground_truth_files = sorted(GROUND_TRUTH_DIR.glob("*.json"))
    if not ground_truth_files:
        raise SystemExit(f"No ground truth files found in {GROUND_TRUTH_DIR}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RESULTS_DIR / "benchmark_run.log"

    summaries = [
        _score_strategy(
            spec=STRATEGY_SPECS[strategy],
            ground_truth_files=ground_truth_files,
            image_dir=args.image_dir,
            env_path=args.env,
            run_all=args.run_all,
            run_missing=args.run_missing,
            log_path=log_path,
        )
        for strategy in args.strategies
    ]

    report_path = RESULTS_DIR / "benchmark_report.md"
    report_path.write_text(format_summary_markdown(summaries), encoding="utf-8")

    json_path = RESULTS_DIR / "benchmark_report.json"
    json_path.write_text(
        json.dumps(
            [_summary_to_dict(summary) for summary in summaries],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print(format_summary_markdown(summaries))
    print(f"Report: {report_path.resolve()}")
    print(f"JSON:   {json_path.resolve()}")
    if log_path.is_file():
        print(f"Log:    {log_path.resolve()}")


if __name__ == "__main__":
    main()
