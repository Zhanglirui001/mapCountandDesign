"""Block-diagram table recognizer entry point."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from time import perf_counter
from uuid import uuid4
from io import BytesIO
from pathlib import Path
from typing import Any, Sequence

from PIL import Image

from count_and_design.application.ai import (
    BoundingBox,
    MinerUOcrService,
    OnnxRuntimeYoloDetector,
)
from count_and_design.application.ai.llm.providers import (
    BedrockLlmProvider,
    OpenRouterLlmProvider,
)
from count_and_design.application.ai.yolo.runtime import (
    BLOCK_SCHEDULE_MODEL_FILENAME,
    BLOCK_SCHEDULE_MODEL_TYPE,
    bundled_yolo_model_dir,
    resolve_yolo_model_path,
)
from count_and_design.application.config import (
    BedrockLlmConfig,
    BlockDiagramTableConfig,
    OpenRouterLlmConfig,
)

from .common import (
    STRATEGY_MINERU_CLAUDE,
    STRATEGY_YOLO_CLAUDE,
    STRATEGY_YOLO_GLM,
    BlockDiagramTableError,
    BlockDiagramTableResult,
    ImageInput,
    TableLocation,
    TableRegion,
    bbox_to_pixels,
    build_result,
    expand_bbox_uniformly,
    extract_table_json_from_image,
    load_image_input,
    scale_bbox_between_spaces,
    select_all_region_blocks,
    select_region_by_iou,
    select_table_bbox,
    select_yolo_table_bbox,
)
from .prompts import BLOCK_DIAGRAM_TABLE_VISION_PROMPT

# MinerU expresses layout bboxes in a 0..1000 page space, while YOLO boxes are
# normalized 0..1. IoU matching needs both in the same space.
LAYOUT_SPACE_SIZE = 1000.0


class BlockDiagramTableRecognizer:
    """Recognize tracker totals from a block-diagram table image."""

    def __init__(
        self,
        config: BlockDiagramTableConfig | None = None,
        mineru: MinerUOcrService | None = None,
        bedrock: BedrockLlmProvider | None = None,
        glm: OpenRouterLlmProvider | None = None,
        *,
        temp_dir: str | Path | None = None,
        block_schedule_detector: OnnxRuntimeYoloDetector | None = None,
    ) -> None:
        self.config = config or BlockDiagramTableConfig.from_env()
        self.mineru = mineru or MinerUOcrService()
        self.bedrock = bedrock
        self.glm = glm
        self._mineru = self.mineru
        self._bedrock = self.bedrock
        self._config = self.config
        self._temp_dir = (
            Path(temp_dir).expanduser() if temp_dir is not None else None
        )
        self._block_schedule_detector = block_schedule_detector

    @classmethod
    def from_env(
        cls,
        env_path: str | Path | None = None,
        **kwargs: Any,
    ) -> BlockDiagramTableRecognizer:
        return cls(
            BlockDiagramTableConfig.from_env(env_path),
            **kwargs,
        )

    def recognize(self, image: ImageInput) -> BlockDiagramTableResult:
        """Locate the table region, then have Claude read it.

        The locate result is a first-class value: extraction only starts once
        a location exists, and any extraction failure carries the location
        (``BlockDiagramTableError.location``) so callers can still draw the
        region the locator found.
        """

        image_path, image_bytes = load_image_input(image)
        location = self.locate(image_path, image_bytes)
        return self.extract_table_from_location(
            image_path,
            image_bytes,
            location,
            strategy=self.config.strategy,
        )

    def locate(
        self,
        image_path: Path | None,
        image_bytes: bytes,
    ) -> TableLocation:
        """Find the table region without calling any LLM."""

        strategy = self.config.strategy
        if strategy == STRATEGY_MINERU_CLAUDE:
            return TableLocation(
                bbox=self.locate_table_bbox(image_path, image_bytes),
                source="mineru",
            )
        if strategy in {STRATEGY_YOLO_CLAUDE, STRATEGY_YOLO_GLM}:
            regions, locate_meta = self.locate_table_bbox_yolo_or_mineru(
                image_path,
                image_bytes,
            )
            return TableLocation(
                bbox=regions[0].bbox,
                source=str(locate_meta.get("table_bbox_source", "")),
                raw=locate_meta,
                regions=tuple(regions),
            )
        raise BlockDiagramTableError(
            f"Unknown block-diagram table strategy: {strategy!r}. "
            f"Expected one of: {STRATEGY_MINERU_CLAUDE!r}, "
            f"{STRATEGY_YOLO_CLAUDE!r}, {STRATEGY_YOLO_GLM!r}."
        )

    def extract_table_from_location(
        self,
        image_path: Path | None,
        image_bytes: bytes,
        location: TableLocation,
        *,
        strategy: str,
    ) -> BlockDiagramTableResult:
        """Crop the located regions and have the vision model extract the rows.

        Every failure after a successful locate is raised with the location
        attached so the found region stays visible in results and debug
        exports even when the LLM call fails.
        """

        try:
            regions = tuple(location.regions) or (
                TableRegion(bbox=location.bbox, kind=location.source),
            )
            stem = image_path.stem if image_path else "block_table"
            cropped_paths = self.crop_regions(
                image_bytes,
                regions,
                stem=stem,
                crop_ratio=1.0,
                suffix="full",
            )
            cropped_images = [path.read_bytes() for path in cropped_paths]
            llm = self._vision_llm(strategy)
            llm_started = perf_counter()
            extraction = extract_table_json_from_image(
                llm,
                cropped_images,
                BLOCK_DIAGRAM_TABLE_VISION_PROMPT,
                bedrock_max_transport_bytes=(
                    self.config.bedrock_max_transport_bytes
                ),
                provider_name=(
                    "GLM" if strategy == STRATEGY_YOLO_GLM else "Claude"
                ),
            )
            llm_seconds = perf_counter() - llm_started
            raw_output = dict(extraction.raw_output)
            raw_output.update(location.raw)
            raw_output["table_bbox_source"] = location.source
            raw_output["table_bboxes"] = [
                [
                    region.bbox.x1,
                    region.bbox.y1,
                    region.bbox.x2,
                    region.bbox.y2,
                ]
                for region in regions
            ]
            raw_output["table_region_kinds"] = [region.kind for region in regions]
            raw_output["table_crop_paths"] = [str(path) for path in cropped_paths]
            timing_key = (
                "glm_seconds"
                if strategy == STRATEGY_YOLO_GLM
                else "claude_seconds"
            )
            raw_output[timing_key] = round(llm_seconds, 3)
            raw_output["total_seconds"] = round(
                float(location.raw.get("yolo_seconds", 0.0))
                + float(location.raw.get("mineru_seconds", 0.0))
                + llm_seconds,
                3,
            )
            return build_result(
                extraction.payload,
                strategy=strategy,
                table_bbox=location.bbox,
                cropped_image_path=cropped_paths[0],
                raw_output=raw_output,
            )
        except BlockDiagramTableError as exc:
            raise exc.with_location(location) from exc

    def recognize_to_json(self, image: ImageInput) -> str:
        return self.recognize(image).to_json()

    def locate_table_bbox(
        self,
        image_path: Path | None,
        image_bytes: bytes,
    ) -> BoundingBox:
        layout = self.parse_page_layout(image_path, image_bytes)
        return select_table_bbox(layout.content_list, layout.markdown)

    def locate_table_bbox_yolo_or_mineru(
        self,
        image_path: Path | None,
        image_bytes: bytes,
    ) -> tuple[list[TableRegion], dict[str, Any]]:
        """Locate table regions with YOLO, falling back to MinerU.

        Three outcomes:

        - YOLO's best box reaches the fallback threshold: use it directly.
        - YOLO scored below the threshold: keep that box as a pointer and take
          the MinerU layout block overlapping it the most, whatever its type.
        - YOLO found nothing at all: hand every MinerU block to the extraction
          model as ordered crops, without filtering.
        """

        with Image.open(BytesIO(image_bytes)) as image:
            width, height = image.size
        yolo_started = perf_counter()
        detections = self._schedule_detector().detect(
            image_bytes,
            confidence=self.config.yolo_detection_confidence,
        )
        yolo_seconds = perf_counter() - yolo_started
        table_bbox, yolo_confidence = select_yolo_table_bbox(
            detections,
            width,
            height,
            min_confidence=self.config.yolo_fallback_confidence,
        )
        low_confidence_bbox, low_confidence = select_yolo_table_bbox(
            detections,
            width,
            height,
            min_confidence=0.0,
        )
        meta: dict[str, Any] = {
            "yolo_seconds": round(yolo_seconds, 3),
            "mineru_seconds": 0.0,
            "yolo_confidence": yolo_confidence,
        }
        if table_bbox is not None:
            table_bbox = self._expand_yolo_bbox(table_bbox)
            meta["table_bbox_source"] = "yolo"
            meta["yolo_bbox_expansion_ratio"] = (
                self.config.yolo_bbox_expansion_ratio
            )
            meta["region_selection"] = "yolo_box"
            meta["region_count"] = 1
            return [TableRegion(bbox=table_bbox, kind="yolo")], meta
        mineru_started = perf_counter()
        try:
            regions = self._locate_mineru_regions(
                image_path,
                image_bytes,
                low_confidence_bbox,
                meta,
            )
        except Exception as exc:
            fallback = self._yolo_fallback_after_mineru_failure(
                meta,
                low_confidence_bbox,
                low_confidence,
                yolo_confidence,
            )
            if fallback is not None:
                return fallback
            raise self._mineru_fallback_error(
                self._yolo_rejection_note(yolo_confidence),
                exc,
            ) from exc
        if regions is None:
            # The layout parsed fine but exposed no usable block. Keep the
            # rejected YOLO box when there is one; otherwise there is nothing
            # left to locate the table with.
            fallback = self._yolo_fallback_after_mineru_failure(
                meta,
                low_confidence_bbox,
                low_confidence,
                yolo_confidence,
                mineru_failed=False,
            )
            if fallback is not None:
                return fallback
            raise self._mineru_fallback_error(
                self._yolo_rejection_note(yolo_confidence),
                BlockDiagramTableError(
                    "MinerU layout contained no block with a usable bbox"
                ),
            )
        meta["mineru_seconds"] = round(perf_counter() - mineru_started, 3)
        meta["table_bbox_source"] = "mineru"
        return regions, meta

    def _locate_mineru_regions(
        self,
        image_path: Path | None,
        image_bytes: bytes,
        low_confidence_bbox: BoundingBox | None,
        meta: dict[str, Any],
    ) -> list[TableRegion] | None:
        """Choose MinerU regions for whichever fallback shape applies.

        Returns ``None`` when the layout exposes no usable block, which lets
        the caller fall back to the rejected YOLO box instead of failing.
        """

        layout = self.parse_page_layout(image_path, image_bytes)
        content_list = list(getattr(layout, "content_list", ()) or ())
        try:
            if low_confidence_bbox is None:
                regions = select_all_region_blocks(content_list)
                meta["region_selection"] = "all_mineru_blocks"
                meta["region_count"] = len(regions)
                return regions
            reference = scale_bbox_between_spaces(
                low_confidence_bbox,
                source_width=1.0,
                source_height=1.0,
                target_width=LAYOUT_SPACE_SIZE,
                target_height=LAYOUT_SPACE_SIZE,
            )
            regions = select_region_by_iou(content_list, reference)
        except BlockDiagramTableError:
            return None
        meta["region_selection"] = "mineru_iou_with_yolo"
        meta["region_kind"] = regions[0].kind
        meta["region_count"] = len(regions)
        return regions

    def _vision_llm(
        self,
        strategy: str,
    ) -> BedrockLlmProvider | OpenRouterLlmProvider:
        if strategy == STRATEGY_YOLO_GLM:
            if self.glm is not None:
                return self.glm
            return OpenRouterLlmProvider(
                replace(
                    OpenRouterLlmConfig.from_env(),
                    model=self.config.openrouter_model,
                )
            )
        return self.bedrock or BedrockLlmProvider(BedrockLlmConfig.from_env())

    def _yolo_rejection_note(self, yolo_confidence: float | None) -> str:
        """Describe why the YOLO result was not accepted directly."""

        if yolo_confidence is None:
            return "YOLO detected no table"
        return (
            f"YOLO's best table detection scored {yolo_confidence:.1%}, below "
            f"the configured {self.config.yolo_fallback_confidence:.1%} "
            "fallback threshold"
        )

    def _mineru_fallback_error(
        self,
        yolo_note: str,
        exc: Exception,
    ) -> BlockDiagramTableError:
        return BlockDiagramTableError(
            f"{yolo_note}; the MinerU fallback failed: {exc}"
        )

    def _yolo_fallback_after_mineru_failure(
        self,
        meta: dict[str, Any],
        low_confidence_bbox: BoundingBox | None,
        low_confidence: float | None,
        yolo_confidence: float | None,
        *,
        mineru_failed: bool = True,
    ) -> tuple[list[TableRegion], dict[str, Any]] | None:
        if low_confidence_bbox is None:
            return None
        meta["table_bbox_source"] = "yolo"
        if mineru_failed:
            meta["mineru_failed"] = True
        meta["region_selection"] = "yolo_box_after_mineru_failure"
        meta["region_count"] = 1
        meta["yolo_bbox_expansion_ratio"] = self.config.yolo_bbox_expansion_ratio
        if yolo_confidence is None:
            meta["yolo_confidence"] = low_confidence
        return [
            TableRegion(
                bbox=self._expand_yolo_bbox(low_confidence_bbox),
                kind="yolo",
            )
        ], meta

    def _expand_yolo_bbox(self, bbox: BoundingBox) -> BoundingBox:
        """Expand the persisted YOLO region so crop and UI overlay stay aligned."""

        return expand_bbox_uniformly(
            bbox,
            expansion_ratio=self.config.yolo_bbox_expansion_ratio,
            max_width=1.0,
            max_height=1.0,
        )

    def _schedule_detector(self) -> OnnxRuntimeYoloDetector:
        if self._block_schedule_detector is None:
            model_path = resolve_yolo_model_path(
                bundled_yolo_model_dir(),
                BLOCK_SCHEDULE_MODEL_TYPE,
                BLOCK_SCHEDULE_MODEL_FILENAME,
            )
            self._block_schedule_detector = OnnxRuntimeYoloDetector(
                model_path,
                confidence=self.config.yolo_detection_confidence,
                source=f"onnxruntime:{BLOCK_SCHEDULE_MODEL_TYPE}",
            )
        return self._block_schedule_detector

    def parse_page_layout(
        self,
        image_path: Path | None,
        image_bytes: bytes,
    ):
        return self.mineru.parse_file(
            image_bytes,
            filename=image_path.name if image_path else "block.png",
            parse_method="ocr",
            table_enable=True,
            return_content_list=True,
        )

    def crop_table(
        self,
        image_bytes: bytes,
        bbox: BoundingBox,
        *,
        stem: str,
        crop_ratio: float,
        suffix: str,
    ) -> Path:
        with Image.open(BytesIO(image_bytes)) as image:
            width, height = image.size
            pixel_box = bbox_to_pixels(bbox, width, height)
            crop_width = max(
                1,
                int((pixel_box.x2 - pixel_box.x1) * crop_ratio),
            )
            cropped = image.crop(
                (
                    int(pixel_box.x1),
                    int(pixel_box.y1),
                    int(pixel_box.x1 + crop_width),
                    int(pixel_box.y2),
                )
            )
            output_dir = self._temp_dir or Path(tempfile.gettempdir())
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / (
                f"{stem}_{uuid4().hex}_table_{suffix}.png"
            )
            cropped.save(output_path, format="PNG")
            return output_path

    def crop_regions(
        self,
        image_bytes: bytes,
        regions: Sequence[TableRegion],
        *,
        stem: str,
        crop_ratio: float,
        suffix: str,
    ) -> list[Path]:
        """Crop every located region, in the order the model should read them."""

        return [
            self.crop_table(
                image_bytes,
                region.bbox,
                stem=stem,
                crop_ratio=crop_ratio,
                suffix=f"{suffix}_{index}" if len(regions) > 1 else suffix,
            )
            for index, region in enumerate(regions)
        ]

    _parse_page_layout = parse_page_layout
    _locate_table_bbox = locate_table_bbox
    _crop_table = crop_table

    def _load_image_input(self, image: ImageInput):
        return load_image_input(image)
