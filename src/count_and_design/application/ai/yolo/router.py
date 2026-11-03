"""Model routing, default configuration, and compatibility helpers."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from threading import BoundedSemaphore, RLock
from typing import Any

from PIL import Image

from ..types import DetectionResult
from .result import detection_result_to_dict
from .runtime import (
    BOX_MODEL_FILENAME,
    BOX_MODEL_TYPE,
    DEFAULT_NMS_IOU_THRESHOLD,
    DEFAULT_TRACKER_NMS_IOU_THRESHOLD,
    TRACKER_MODEL_FILENAME,
    TRACKER_MODEL_TYPE,
    ImageSource,
    OnnxRuntimeYoloDetector,
    UltralyticsYoloDetector,
    YoloDetectorError,
    YoloModelConfig,
    YoloModelType,
    bundled_yolo_model_dir,
    resolve_yolo_model_path,
)


class YoloDetectorRouter:
    """Select and cache a YOLO detector by business model type."""

    def __init__(self, configs: Mapping[YoloModelType, YoloModelConfig]) -> None:
        if not configs:
            raise YoloDetectorError("At least one YOLO model config is required.")
        self._configs = {
            str(model_type): config
            for model_type, config in configs.items()
        }
        self._detectors: dict[str, Any] = {}
        self._lock = RLock()
        self._inference_slots = BoundedSemaphore(1)
        self._parallelism = (1, max(1, os.cpu_count() or 1))

    @property
    def model_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._configs))

    def detect(
        self,
        model_type: YoloModelType,
        image: ImageSource,
        *,
        confidence: float | None = None,
    ) -> list[DetectionResult]:
        with self._inference_slots:
            return self._detector_for(model_type).detect(
                image,
                confidence=confidence,
            )

    def configure_parallelism(self, page_count: int) -> tuple[int, int]:
        """Size ONNX pools from page count and the logical CPU budget."""

        from count_and_design.application.env import env_int_or_none

        cpu_count = max(1, os.cpu_count() or 1)
        configured = env_int_or_none("AGENT3_YOLO_WORKERS", None)
        desired = configured if configured is not None and configured > 0 else max(
            1, cpu_count // 4
        )
        workers = min(max(1, page_count), cpu_count, desired)
        intra_op_threads = max(1, cpu_count // workers)
        with self._lock:
            if self._parallelism == (workers, intra_op_threads):
                return self._parallelism
            self._parallelism = (workers, intra_op_threads)
            self._inference_slots = BoundedSemaphore(workers)
            for detector in self._detectors.values():
                configure = getattr(detector, "configure_parallelism", None)
                if callable(configure):
                    configure(workers, intra_op_threads)
        return self._parallelism

    def warmup(self) -> None:
        """Load every routed model and absorb first-inference cold-start."""

        for key in sorted(self._configs):
            self._detector_for(key).warmup()

    def model_path_for(self, model_type: YoloModelType) -> Path:
        return Path(self._config_for(model_type).path).expanduser()

    def _detector_for(
        self,
        model_type: YoloModelType,
    ) -> Any:
        key = str(model_type)
        with self._lock:
            if key not in self._detectors:
                config = self._config_for(key)
                model_path = Path(config.path).expanduser()
                if (
                    config.use_onnxruntime
                    and model_path.suffix.casefold() == ".onnx"
                    and config.device in {None, "cpu"}
                ):
                    detector = OnnxRuntimeYoloDetector(
                        model_path,
                        labels=config.labels,
                        confidence=config.confidence,
                        iou=config.iou,
                        source=f"onnxruntime:{key}",
                    )
                    detector.configure_parallelism(*self._parallelism)
                else:
                    detector = UltralyticsYoloDetector(
                        config.path,
                        labels=config.labels,
                        confidence=config.confidence,
                        iou=config.iou,
                        device=config.device,
                        source=f"ultralytics:{key}",
                    )
                self._detectors[key] = detector
            return self._detectors[key]

    def _config_for(self, model_type: YoloModelType) -> YoloModelConfig:
        key = str(model_type)
        try:
            return self._configs[key]
        except KeyError as exc:
            expected = ", ".join(self.model_types)
            raise YoloDetectorError(
                f"Unknown YOLO model type: {key}. Expected one of: {expected}"
            ) from exc


def detect_with_yolo(
    model_path: str | Path,
    image: ImageSource,
    *,
    labels: Mapping[int, str] | None = None,
    confidence: float = 0.25,
    iou: float | None = None,
    device: str | None = None,
) -> list[DetectionResult]:
    """Compatibility helper for one detection with an explicit model."""

    return UltralyticsYoloDetector(
        model_path,
        labels=labels,
        confidence=confidence,
        iou=iou,
        device=device,
    ).detect(image)


def detect_yolo(
    model_type: YoloModelType,
    image: ImageSource,
    *,
    confidence: float = 0.25,
    iou: float | None = None,
    device: str | None = None,
    model_dir: str | Path | None = None,
) -> list[DetectionResult]:
    """Compatibility helper using a bundled YOLO model."""

    router = create_yolo_router(
        model_dir,
        confidence=confidence,
        iou=iou,
        device=device,
    )
    return router.detect(model_type, image)


def detect_yolo_to_json(
    model_type: YoloModelType,
    image: ImageSource,
    output_path: str | Path,
    *,
    confidence: float = 0.25,
    iou: float | None = None,
    device: str | None = None,
    model_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Compatibility helper that writes one detection result as JSON."""

    router = create_yolo_router(
        model_dir,
        confidence=confidence,
        iou=iou,
        device=device,
    )
    detections = router.detect(model_type, image)
    image_text = _image_text(image)
    payload = {
        "model_type": str(model_type),
        "model_path": str(router.model_path_for(model_type)),
        "image": image_text,
        "confidence": confidence,
        "device": device,
        "detections": [
            detection_result_to_dict(item)
            for item in detections
        ],
    }
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def _image_text(image: ImageSource) -> str:
    if isinstance(image, bytes):
        return "<bytes>"
    if isinstance(image, Image.Image):
        return f"<PIL Image {image.width}x{image.height}>"
    if isinstance(image, (str, Path)):
        return str(Path(image).expanduser())
    return f"<{type(image).__name__}>"


def default_yolo_configs(
    model_dir: str | Path | None = None,
    *,
    confidence: float = 0.25,
    iou: float | None = None,
    device: str | None = None,
    use_onnxruntime: bool = True,
) -> dict[str, YoloModelConfig]:
    """Build model configs for bundled tracker and box models."""

    base_dir = (
        bundled_yolo_model_dir()
        if model_dir is None
        else Path(model_dir).expanduser()
    )
    tracker_iou = DEFAULT_TRACKER_NMS_IOU_THRESHOLD if iou is None else iou
    box_iou = DEFAULT_NMS_IOU_THRESHOLD if iou is None else iou
    return {
        TRACKER_MODEL_TYPE: YoloModelConfig(
            resolve_yolo_model_path(
                base_dir,
                TRACKER_MODEL_TYPE,
                TRACKER_MODEL_FILENAME,
            ),
            confidence=confidence,
            iou=tracker_iou,
            device=device,
            use_onnxruntime=use_onnxruntime,
        ),
        BOX_MODEL_TYPE: YoloModelConfig(
            resolve_yolo_model_path(
                base_dir,
                BOX_MODEL_TYPE,
                BOX_MODEL_FILENAME,
            ),
            confidence=confidence,
            iou=box_iou,
            device=device,
            use_onnxruntime=use_onnxruntime,
        ),
    }


def create_yolo_router(
    model_dir: str | Path | None = None,
    *,
    confidence: float = 0.25,
    iou: float | None = None,
    device: str | None = None,
    use_onnxruntime: bool | None = None,
) -> YoloDetectorRouter:
    direct_onnx = model_dir is None if use_onnxruntime is None else use_onnxruntime
    return YoloDetectorRouter(
        default_yolo_configs(
            model_dir,
            confidence=confidence,
            iou=iou,
            device=device,
            use_onnxruntime=direct_onnx,
        )
    )
