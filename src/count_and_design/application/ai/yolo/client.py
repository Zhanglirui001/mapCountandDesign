"""Simple, reusable facade for local YOLO detection."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from PIL import Image

from .result import YoloResult
from .router import YoloDetectorRouter, create_yolo_router
from .runtime import BOX_MODEL_TYPE, ImageSource, YoloModelType


YoloProgressCallback = Callable[[int, int], None]


class YoloClient:
    """Detect with bundled YOLO models while caching loaded runtimes."""

    def __init__(self, router: YoloDetectorRouter | None = None) -> None:
        self._router = router or create_yolo_router()

    @classmethod
    def from_defaults(
        cls,
        *,
        model_dir: str | Path | None = None,
        confidence: float = 0.25,
        iou: float | None = None,
        device: str | None = None,
    ) -> "YoloClient":
        return cls(
            create_yolo_router(
                model_dir,
                confidence=confidence,
                iou=iou,
                device=device,
            )
        )

    @property
    def model_types(self) -> tuple[str, ...]:
        return self._router.model_types

    def warmup(self) -> None:
        """Preload every routed model so the first detect skips cold-start."""

        self._router.warmup()

    def configure_parallelism(self, page_count: int) -> tuple[int, int]:
        """Configure bounded CPU inference pools for an Agent3 run."""

        return self._router.configure_parallelism(page_count)

    def detect(
        self,
        image: ImageSource,
        *,
        model_type: YoloModelType = BOX_MODEL_TYPE,
        confidence: float | None = None,
    ) -> YoloResult:
        return YoloResult(
            model_type=str(model_type),
            model_path=str(self._router.model_path_for(model_type)),
            image=_image_description(image),
            detections=self._router.detect(
                model_type,
                image,
                confidence=confidence,
            ),
        )

    def detect_images(
        self,
        images: Sequence[ImageSource],
        *,
        model_type: YoloModelType = BOX_MODEL_TYPE,
        confidence: float | None = None,
        on_progress: YoloProgressCallback | None = None,
    ) -> list[YoloResult]:
        """Detect images in order while reusing the loaded model."""

        results: list[YoloResult] = []
        total = len(images)
        for completed, image in enumerate(images, start=1):
            results.append(
                self.detect(
                    image,
                    model_type=model_type,
                    confidence=confidence,
                )
            )
            if on_progress is not None:
                on_progress(completed, total)
        return results


def _image_description(image: ImageSource) -> str:
    if isinstance(image, bytes):
        return "<bytes>"
    if isinstance(image, Image.Image):
        return f"<PIL Image {image.width}x{image.height}>"
    if isinstance(image, (str, Path)):
        return str(Path(image).expanduser())
    # Pre-decoded sources such as numpy arrays: describe without crashing.
    return f"<{type(image).__name__}>"
