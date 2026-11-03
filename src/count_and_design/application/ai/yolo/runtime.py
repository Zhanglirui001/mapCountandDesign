"""Ultralytics runtime adapter and local YOLO model definitions."""

from __future__ import annotations

import ast
import logging
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from queue import LifoQueue
from threading import Lock
from time import perf_counter
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

from ..types import BoundingBox, DetectionResult


ImageSource = str | Path | bytes | Any
YoloModelType = str

TRACKER_MODEL_TYPE = "tracker"
BOX_MODEL_TYPE = "box"
BLOCK_SCHEDULE_MODEL_TYPE = "block_schedule"
OVERALL_SCHEDULE_MODEL_TYPE = "overall_schedule"
TRACKER_MODEL_FILENAME = "node_tracker_yolov8n.onnx"
BOX_MODEL_FILENAME = "box_yolov8n.onnx"
BLOCK_SCHEDULE_MODEL_FILENAME = "block_schedule_yolov26s.onnx"
OVERALL_SCHEDULE_MODEL_FILENAME = "overall_schedule_yolov26s.onnx"
DEFAULT_NMS_IOU_THRESHOLD = 0.7
# Tracker instances may touch, but overlap above the Agent3 ambiguity limit is
# a duplicate candidate rather than a distinct tracker.
DEFAULT_TRACKER_NMS_IOU_THRESHOLD = 0.3


class YoloDetectorError(RuntimeError):
    """Raised when local YOLO inference cannot be executed."""


def bundled_yolo_model_dir() -> Path:
    """Return the packaged directory containing bundled YOLO models."""

    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            return Path(bundle_root) / "count_and_design" / "resources" / "models"
    return Path(__file__).resolve().parents[3] / "resources" / "models"


def resolve_yolo_model_path(
    model_dir: str | Path,
    model_type: YoloModelType,
    preferred_filename: str,
) -> Path:
    """Resolve a local ONNX model path from the model directory."""

    base_dir = Path(model_dir).expanduser()
    preferred_path = base_dir / preferred_filename
    if preferred_path.exists():
        return preferred_path

    model_type_text = str(model_type).lower()
    candidates = sorted(
        path
        for path in base_dir.glob("*.onnx")
        if model_type_text in path.stem.lower()
    )
    if not candidates:
        return preferred_path

    preferred_prefixes = (
        f"{model_type_text}_",
        f"{model_type_text}-",
        f"node_{model_type_text}_",
        f"node-{model_type_text}-",
    )
    for candidate in candidates:
        stem = candidate.stem.lower()
        if stem == model_type_text or stem.startswith(preferred_prefixes):
            return candidate

    if len(candidates) == 1:
        return candidates[0]

    names = ", ".join(path.name for path in candidates)
    raise YoloDetectorError(
        f"Multiple YOLO {model_type_text} models found in {base_dir}: {names}"
    )


@dataclass(frozen=True)
class YoloModelConfig:
    """Configuration for one local YOLO model variant."""

    path: str | Path
    labels: Mapping[int, str] = field(default_factory=dict)
    confidence: float = 0.25
    iou: float | None = None
    device: str | None = None
    use_onnxruntime: bool = False


class UltralyticsYoloDetector:
    """Run local YOLO inference through a lazily loaded runtime."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        labels: Mapping[int, str] | None = None,
        confidence: float = 0.25,
        iou: float | None = None,
        device: str | None = None,
        source: str = "ultralytics",
    ) -> None:
        self._model_path = Path(model_path).expanduser()
        self._labels = dict(labels or {})
        self._confidence = confidence
        self._iou = DEFAULT_NMS_IOU_THRESHOLD if iou is None else iou
        self._device = device
        self._source = source
        self._model: Any | None = None

    @property
    def model_path(self) -> Path:
        return self._model_path

    def warmup(self, *, image: Image.Image | None = None) -> None:
        """Load the runtime and finish one tiny inference to absorb cold-start.

        The first predict pays torch/ultralytics imports, ONNX Runtime session
        creation and backend JIT; callers can run this ahead of real work so
        those costs leave the recognition critical path.
        """

        model = self._load_model()
        probe = image if image is not None else Image.new("RGB", (640, 640), "white")
        try:
            kwargs: dict[str, Any] = {
                "source": probe,
                "conf": self._confidence,
                "iou": self._iou,
                "verbose": False,
            }
            if self._device:
                kwargs["device"] = self._device
            model.predict(**kwargs)
        except Exception as exc:  # pragma: no cover - backend-specific detail
            raise YoloDetectorError(f"YOLO warmup failed: {exc}") from exc

    def detect(
        self,
        image: ImageSource,
        *,
        confidence: float | None = None,
    ) -> list[DetectionResult]:
        model = self._load_model()
        threshold = self._confidence if confidence is None else confidence
        source, cleanup_path = self._prepare_source(image)
        try:
            kwargs: dict[str, Any] = {
                "source": source,
                "conf": threshold,
                "verbose": False,
            }
            kwargs["iou"] = self._iou
            if self._device:
                kwargs["device"] = self._device
            return non_maximum_suppression(
                self._normalize_results(model.predict(**kwargs), threshold),
                self._iou,
            )
        except Exception as exc:  # pragma: no cover - backend-specific detail
            raise YoloDetectorError(f"YOLO inference failed: {exc}") from exc
        finally:
            if cleanup_path is not None:
                try:
                    os.unlink(cleanup_path)
                except OSError:
                    pass

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        if not self._model_path.exists():
            raise YoloDetectorError(
                f"YOLO model file does not exist: {self._model_path}"
            )
        try:
            from ultralytics import YOLO
        except ImportError as exc:  # pragma: no cover
            raise YoloDetectorError(
                "Ultralytics runtime could not be imported. Install "
                "count-and-design[ai] and its packaged dependencies: "
                f"{exc}"
            ) from exc
        try:
            self._model = YOLO(str(self._model_path), task="detect")
        except Exception as exc:  # pragma: no cover
            raise YoloDetectorError(f"Could not load YOLO model: {exc}") from exc
        return self._model

    def _prepare_source(self, image: ImageSource) -> tuple[Any, str | None]:
        # Pre-decoded PIL images are forwarded as-is so callers that
        # already paid the decode cost (large drawing PNGs) never pay it
        # twice.
        if isinstance(image, Image.Image):
            return image, None
        if isinstance(image, bytes):
            handle = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
            try:
                handle.write(image)
                return handle.name, handle.name
            finally:
                handle.close()
        source = (
            str(Path(image).expanduser())
            if isinstance(image, Path)
            else str(image)
        )
        if not source:
            raise YoloDetectorError("Image source is empty.")
        return source, None

    def _normalize_results(
        self,
        results: Any,
        threshold: float,
    ) -> list[DetectionResult]:
        detections: list[DetectionResult] = []
        for result in results or []:
            names = self._result_names(result)
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            xyxy = self._to_list(getattr(boxes, "xyxy", []))
            confidences = self._to_list(getattr(boxes, "conf", []))
            class_ids = self._to_list(getattr(boxes, "cls", []))
            for index, coords in enumerate(xyxy):
                if len(coords) < 4:
                    continue
                score = (
                    float(confidences[index])
                    if index < len(confidences)
                    else 0.0
                )
                if score < threshold:
                    continue
                class_id = self._class_id_at(class_ids, index)
                detections.append(
                    DetectionResult(
                        label=self._label_for(class_id, names),
                        confidence=score,
                        class_id=class_id,
                        box=BoundingBox(
                            float(coords[0]),
                            float(coords[1]),
                            float(coords[2]),
                            float(coords[3]),
                        ),
                        source=self._source,
                    )
                )
        return detections

    def _result_names(self, result: Any) -> Mapping[int, str]:
        names = getattr(result, "names", None)
        if isinstance(names, Mapping):
            return {int(key): str(value) for key, value in names.items()}
        return self._labels

    def _label_for(
        self,
        class_id: int | None,
        names: Mapping[int, str],
    ) -> str:
        if class_id is None:
            return "unknown"
        return self._labels.get(class_id) or names.get(class_id) or str(class_id)

    @staticmethod
    def _class_id_at(class_ids: list[Any], index: int) -> int | None:
        if index >= len(class_ids):
            return None
        try:
            return int(class_ids[index])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_list(value: Any) -> list[Any]:
        if hasattr(value, "cpu"):
            value = value.cpu()
        if hasattr(value, "tolist"):
            value = value.tolist()
        return list(value or [])


class OnnxRuntimeYoloDetector:
    """Direct ONNX Runtime detector with a bounded pool of CPU sessions."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        labels: Mapping[int, str] | None = None,
        confidence: float = 0.25,
        iou: float | None = None,
        source: str = "onnxruntime",
    ) -> None:
        self._model_path = Path(model_path).expanduser()
        self._labels = dict(labels or {})
        self._confidence = confidence
        self._iou = DEFAULT_NMS_IOU_THRESHOLD if iou is None else iou
        self._source = source
        self._workers = 1
        self._intra_op_threads = max(1, os.cpu_count() or 1)
        self._sessions: LifoQueue[Any] | None = None
        self._session_lock = Lock()
        self._input_size: tuple[int, int] | None = None
        self._model_labels: dict[int, str] = {}

    @property
    def model_path(self) -> Path:
        return self._model_path

    def configure_parallelism(self, workers: int, intra_op_threads: int) -> None:
        workers = max(1, int(workers))
        intra_op_threads = max(1, int(intra_op_threads))
        with self._session_lock:
            if (
                workers == self._workers
                and intra_op_threads == self._intra_op_threads
            ):
                return
            self._workers = workers
            self._intra_op_threads = intra_op_threads
            # Configuration occurs before page tasks are submitted. Dropping
            # the old queue safely releases any warmup-created sessions.
            self._sessions = None
            self._input_size = None
            self._model_labels = {}

    def warmup(self, *, image: Image.Image | None = None) -> None:
        del image
        self._ensure_sessions()

    def detect(
        self,
        image: ImageSource,
        *,
        confidence: float | None = None,
    ) -> list[DetectionResult]:
        started = perf_counter()
        sessions = self._ensure_sessions()
        session_ready = perf_counter()
        session = sessions.get()
        try:
            threshold = self._confidence if confidence is None else confidence
            tensor, source_size, scale, padding = self._preprocess(image)
            preprocessed = perf_counter()
            input_name = session.get_inputs()[0].name
            raw = session.run(None, {input_name: tensor})[0]
            inferred = perf_counter()
            detections = self._postprocess(
                raw,
                source_size=source_size,
                scale=scale,
                padding=padding,
                threshold=threshold,
            )
            logger.info(
                "yolo.detect size=%sx%s session=%.3f preprocess=%.3f "
                "infer=%.3f boxes=%d source=%s",
                source_size[0],
                source_size[1],
                session_ready - started,
                preprocessed - session_ready,
                inferred - preprocessed,
                len(detections),
                self._source,
            )
            return detections
        except YoloDetectorError:
            raise
        except Exception as exc:
            raise YoloDetectorError(f"YOLO inference failed: {exc}") from exc
        finally:
            sessions.put(session)

    def _ensure_sessions(self) -> LifoQueue[Any]:
        if self._sessions is not None:
            return self._sessions
        with self._session_lock:
            if self._sessions is not None:
                return self._sessions
            if not self._model_path.exists():
                raise YoloDetectorError(
                    f"YOLO model file does not exist: {self._model_path}"
                )
            try:
                import onnxruntime as ort
            except ImportError as exc:  # pragma: no cover
                raise YoloDetectorError(
                    f"ONNX Runtime could not be imported: {exc}"
                ) from exc
            queue: LifoQueue[Any] = LifoQueue(maxsize=self._workers)
            try:
                for _ in range(self._workers):
                    options = ort.SessionOptions()
                    options.intra_op_num_threads = self._intra_op_threads
                    options.inter_op_num_threads = 1
                    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                    session = ort.InferenceSession(
                        str(self._model_path),
                        sess_options=options,
                        providers=["CPUExecutionProvider"],
                    )
                    self._read_session_metadata(session)
                    queue.put(session)
            except Exception as exc:
                raise YoloDetectorError(
                    f"Could not load YOLO ONNX model: {exc}"
                ) from exc
            self._sessions = queue
            return queue

    def _read_session_metadata(self, session: Any) -> None:
        shape = session.get_inputs()[0].shape
        if (
            len(shape) != 4
            or not isinstance(shape[2], int)
            or not isinstance(shape[3], int)
        ):
            raise YoloDetectorError("YOLO ONNX model requires a fixed NCHW input.")
        size = (int(shape[3]), int(shape[2]))
        if self._input_size is not None and self._input_size != size:
            raise YoloDetectorError("YOLO session pool has inconsistent input sizes.")
        self._input_size = size
        raw_names = session.get_modelmeta().custom_metadata_map.get("names", "")
        if raw_names and not self._model_labels:
            try:
                parsed = ast.literal_eval(raw_names)
            except (SyntaxError, ValueError):
                parsed = {}
            if isinstance(parsed, Mapping):
                self._model_labels = {
                    int(key): str(value) for key, value in parsed.items()
                }

    def _load_rgb(self, image: ImageSource) -> Image.Image:
        if isinstance(image, Image.Image):
            return image.convert("RGB")
        try:
            if isinstance(image, bytes):
                from io import BytesIO

                with Image.open(BytesIO(image)) as loaded:
                    return loaded.convert("RGB")
            with Image.open(Path(image).expanduser()) as loaded:
                return loaded.convert("RGB")
        except Exception as exc:
            raise YoloDetectorError(f"Could not read YOLO image: {exc}") from exc

    def _preprocess(
        self,
        image: ImageSource,
    ) -> tuple[Any, tuple[int, int], float, tuple[int, int]]:
        import numpy as np

        source = self._load_rgb(image)
        source_width, source_height = source.size
        assert self._input_size is not None
        input_width, input_height = self._input_size
        scale = min(input_width / source_width, input_height / source_height)
        resized_size = (
            max(1, round(source_width * scale)),
            max(1, round(source_height * scale)),
        )
        # Agent2/Agent3 drawings are often 6000px. The exported model is a
        # fixed square (640/1280 for the currently bundled models), so bilinear
        # letterbox of tens of megapixels dominates table location. BOX is
        # the correct downscale filter and much cheaper; boxes still map
        # back through `scale` to the original page.
        resample = (
            Image.Resampling.BOX
            if max(source_width, source_height) > 2 * max(resized_size)
            else Image.Resampling.BILINEAR
        )
        resized = source.resize(resized_size, resample)
        left = round((input_width - resized.width) / 2 - 0.1)
        top = round((input_height - resized.height) / 2 - 0.1)
        canvas = Image.new("RGB", (input_width, input_height), (114, 114, 114))
        canvas.paste(resized, (left, top))
        tensor = np.asarray(canvas, dtype=np.float32)
        tensor = np.ascontiguousarray(tensor.transpose(2, 0, 1)[None] / 255.0)
        source.close()
        resized.close()
        canvas.close()
        return tensor, (source_width, source_height), scale, (left, top)

    def _postprocess(
        self,
        raw: Any,
        *,
        source_size: tuple[int, int],
        scale: float,
        padding: tuple[int, int],
        threshold: float,
    ) -> list[DetectionResult]:
        import numpy as np

        prediction = np.asarray(raw)
        if prediction.ndim != 3 or prediction.shape[0] != 1:
            raise YoloDetectorError(f"Unsupported YOLO output shape: {prediction.shape}")
        rows = prediction[0].T
        if rows.shape[1] < 5:
            return []
        class_scores = rows[:, 4:]
        class_ids = class_scores.argmax(axis=1)
        scores = class_scores[np.arange(len(rows)), class_ids]
        keep = scores >= threshold
        rows = rows[keep]
        scores = scores[keep]
        class_ids = class_ids[keep]
        if not len(rows):
            return []
        boxes = np.empty((len(rows), 4), dtype=np.float32)
        boxes[:, 0] = rows[:, 0] - rows[:, 2] / 2
        boxes[:, 1] = rows[:, 1] - rows[:, 3] / 2
        boxes[:, 2] = rows[:, 0] + rows[:, 2] / 2
        boxes[:, 3] = rows[:, 1] + rows[:, 3] / 2
        left, top = padding
        boxes[:, [0, 2]] = (boxes[:, [0, 2]] - left) / scale
        boxes[:, [1, 3]] = (boxes[:, [1, 3]] - top) / scale
        width, height = source_size
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, width)
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, height)
        selected = _class_aware_nms(boxes, scores, class_ids, self._iou)
        labels = self._labels or self._model_labels
        return [
            DetectionResult(
                label=labels.get(int(class_ids[index]), str(int(class_ids[index]))),
                confidence=float(scores[index]),
                class_id=int(class_ids[index]),
                box=BoundingBox(*(float(value) for value in boxes[index])),
                source=self._source,
            )
            for index in selected[:300]
        ]


def _class_aware_nms(boxes: Any, scores: Any, class_ids: Any, iou: float) -> list[int]:
    """Pure-numpy class-aware non-maximum suppression."""

    import numpy as np

    selected: list[int] = []
    for class_id in np.unique(class_ids):
        indices = np.where(class_ids == class_id)[0]
        order = indices[np.argsort(scores[indices])[::-1]]
        while len(order):
            current = int(order[0])
            selected.append(current)
            if len(order) == 1:
                break
            rest = order[1:]
            x1 = np.maximum(boxes[current, 0], boxes[rest, 0])
            y1 = np.maximum(boxes[current, 1], boxes[rest, 1])
            x2 = np.minimum(boxes[current, 2], boxes[rest, 2])
            y2 = np.minimum(boxes[current, 3], boxes[rest, 3])
            intersection = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
            current_area = max(0.0, boxes[current, 2] - boxes[current, 0]) * max(
                0.0, boxes[current, 3] - boxes[current, 1]
            )
            rest_area = np.maximum(0, boxes[rest, 2] - boxes[rest, 0]) * np.maximum(
                0, boxes[rest, 3] - boxes[rest, 1]
            )
            union = current_area + rest_area - intersection
            overlaps = np.divide(
                intersection,
                union,
                out=np.zeros_like(intersection),
                where=union > 0,
            )
            order = rest[overlaps <= iou]
    selected.sort(key=lambda index: float(scores[index]), reverse=True)
    return selected


def non_maximum_suppression(
    detections: list[DetectionResult],
    iou_threshold: float = DEFAULT_NMS_IOU_THRESHOLD,
) -> list[DetectionResult]:
    """Keep only the highest-confidence detection for same-class overlaps."""

    if not 0.0 <= iou_threshold <= 1.0:
        raise ValueError("NMS IoU threshold must be between 0 and 1.")
    ordered = sorted(
        enumerate(detections),
        key=lambda item: (-item[1].confidence, item[0]),
    )
    selected: list[DetectionResult] = []
    for _index, candidate in ordered:
        if any(
            _same_detection_class(candidate, kept)
            and _detection_iou(candidate.box, kept.box) > iou_threshold
            for kept in selected
        ):
            continue
        selected.append(candidate)
    return selected


def _same_detection_class(left: DetectionResult, right: DetectionResult) -> bool:
    if left.class_id is not None and right.class_id is not None:
        return left.class_id == right.class_id
    return left.label.casefold() == right.label.casefold()


def _detection_iou(left: BoundingBox, right: BoundingBox) -> float:
    intersection_width = max(0.0, min(left.x2, right.x2) - max(left.x1, right.x1))
    intersection_height = max(0.0, min(left.y2, right.y2) - max(left.y1, right.y1))
    intersection = intersection_width * intersection_height
    left_area = max(0.0, left.x2 - left.x1) * max(0.0, left.y2 - left.y1)
    right_area = max(0.0, right.x2 - right.x1) * max(0.0, right.y2 - right.y1)
    union = left_area + right_area - intersection
    return intersection / union if union > 0.0 else 0.0
