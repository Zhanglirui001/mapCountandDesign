from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from PIL import Image

from count_and_design.application.ai import (
    BOX_MODEL_FILENAME,
    BOX_MODEL_TYPE,
    DEFAULT_TRACKER_NMS_IOU_THRESHOLD,
    TRACKER_MODEL_FILENAME,
    TRACKER_MODEL_TYPE,
    UltralyticsYoloDetector,
    OnnxRuntimeYoloDetector,
    YoloClient,
    YoloDetectorError,
    YoloDetectorRouter,
    YoloModelConfig,
    bundled_yolo_model_dir,
    create_yolo_router,
    default_yolo_configs,
    detect_with_yolo,
    detect_yolo,
    detect_yolo_to_json,
)


class _TensorLike:
    def __init__(self, value):
        self._value = value

    def cpu(self):
        return self

    def tolist(self):
        return self._value


class _FakeYOLO:
    calls: list[tuple[str, dict]] = []
    loaded_paths: list[str] = []
    loaded_options: list[dict] = []

    def __init__(self, model_path: str, **kwargs) -> None:
        self.model_path = model_path
        self.__class__.loaded_paths.append(model_path)
        self.__class__.loaded_options.append(kwargs)

    def predict(self, **kwargs):
        self.__class__.calls.append((self.model_path, kwargs))
        return [
            SimpleNamespace(
                names={0: "INV", 1: "Node"},
                boxes=SimpleNamespace(
                    xyxy=_TensorLike([[1, 2, 30, 40], [5, 6, 7, 8]]),
                    conf=_TensorLike([0.92, 0.12]),
                    cls=_TensorLike([0, 1]),
                ),
            )
        ]


@pytest.fixture(autouse=True)
def fake_ultralytics(monkeypatch):
    _FakeYOLO.calls = []
    _FakeYOLO.loaded_paths = []
    _FakeYOLO.loaded_options = []
    module = ModuleType("ultralytics")
    module.YOLO = _FakeYOLO
    monkeypatch.setitem(sys.modules, "ultralytics", module)


def test_ultralytics_detector_normalizes_detections(tmp_path: Path) -> None:
    model = tmp_path / "model.pt"
    image = tmp_path / "page.png"
    model.write_bytes(b"model")
    image.write_bytes(b"image")
    detector = UltralyticsYoloDetector(model, confidence=0.2, device="cpu")

    detections = detector.detect(image)

    assert len(detections) == 1
    assert detections[0].label == "INV"
    assert detections[0].class_id == 0
    assert detections[0].confidence == pytest.approx(0.92)
    assert detections[0].box.to_tuple() == (1.0, 2.0, 30.0, 40.0)
    assert _FakeYOLO.calls[0][1]["source"] == str(image)
    assert _FakeYOLO.calls[0][1]["conf"] == 0.2
    assert _FakeYOLO.calls[0][1]["iou"] == 0.7
    assert _FakeYOLO.calls[0][1]["device"] == "cpu"


def test_ultralytics_detector_applies_class_aware_nms(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class _OverlappingYOLO:
        def __init__(self, _model_path: str, **_kwargs) -> None:
            pass

        def predict(self, **_kwargs):
            return [
                SimpleNamespace(
                    names={0: "Node", 1: "Tracker"},
                    boxes=SimpleNamespace(
                        xyxy=_TensorLike(
                            [
                                [0, 0, 10, 10],
                                [1, 0, 11, 10],
                                [1, 0, 11, 10],
                            ]
                        ),
                        conf=_TensorLike([0.8, 0.9, 0.7]),
                        cls=_TensorLike([0, 0, 1]),
                    ),
                )
            ]

    module = ModuleType("ultralytics")
    module.YOLO = _OverlappingYOLO
    monkeypatch.setitem(sys.modules, "ultralytics", module)
    model = tmp_path / "model.pt"
    model.write_bytes(b"model")

    detections = UltralyticsYoloDetector(model, iou=0.5).detect(
        tmp_path / "page.png"
    )

    assert [(item.label, item.confidence) for item in detections] == [
        ("Node", pytest.approx(0.9)),
        ("Tracker", pytest.approx(0.7)),
    ]


def test_detect_with_yolo_one_shot_call(tmp_path: Path) -> None:
    model = tmp_path / "model.pt"
    image = tmp_path / "page.png"
    model.write_bytes(b"model")
    image.write_bytes(b"image")

    detections = detect_with_yolo(model, image, labels={0: "Inverter"})

    assert detections[0].label == "Inverter"


def test_detector_rejects_missing_model(tmp_path: Path) -> None:
    detector = UltralyticsYoloDetector(tmp_path / "missing.pt")

    with pytest.raises(YoloDetectorError, match="does not exist"):
        detector.detect(tmp_path / "page.png")


def test_yolo_router_selects_model_by_type_and_caches_detector(tmp_path: Path) -> None:
    tracker_model = tmp_path / "tracker.onnx"
    box_model = tmp_path / "box.onnx"
    image = tmp_path / "page.png"
    tracker_model.write_bytes(b"tracker")
    box_model.write_bytes(b"box")
    image.write_bytes(b"image")
    router = YoloDetectorRouter(
        {
            TRACKER_MODEL_TYPE: YoloModelConfig(
                tracker_model,
                labels={0: "Tracker"},
                confidence=0.33,
            ),
            BOX_MODEL_TYPE: YoloModelConfig(
                box_model,
                labels={0: "Box"},
                confidence=0.44,
                device="cpu",
            ),
        }
    )

    detections = router.detect(BOX_MODEL_TYPE, image)
    router.detect(BOX_MODEL_TYPE, image)

    assert router.model_types == (BOX_MODEL_TYPE, TRACKER_MODEL_TYPE)
    assert detections[0].label == "Box"
    assert detections[0].source == "ultralytics:box"
    assert _FakeYOLO.loaded_paths == [str(box_model)]
    assert _FakeYOLO.loaded_options[0]["task"] == "detect"
    assert _FakeYOLO.calls[0][0] == str(box_model)
    assert _FakeYOLO.calls[0][1]["conf"] == 0.44
    assert _FakeYOLO.calls[0][1]["iou"] == 0.7
    assert _FakeYOLO.calls[0][1]["device"] == "cpu"


def test_yolo_client_returns_structured_result_and_reuses_model(
    tmp_path: Path,
) -> None:
    model = tmp_path / "box.onnx"
    first_image = tmp_path / "first.png"
    second_image = tmp_path / "second.png"
    model.write_bytes(b"model")
    first_image.write_bytes(b"first")
    second_image.write_bytes(b"second")
    client = YoloClient(
        YoloDetectorRouter(
            {
                BOX_MODEL_TYPE: YoloModelConfig(
                    model,
                    confidence=0.2,
                )
            }
        )
    )
    progress: list[tuple[int, int]] = []

    results = client.detect_images(
        [first_image, second_image],
        on_progress=lambda completed, total: progress.append(
            (completed, total)
        ),
    )

    assert len(results) == 2
    assert results[0].model_type == BOX_MODEL_TYPE
    assert results[0].model_path == str(model)
    assert results[0].image == str(first_image)
    assert results[0].to_dict()["detections"][0]["label"] == "INV"
    assert _FakeYOLO.loaded_paths == [str(model)]
    assert progress == [(1, 2), (2, 2)]


def test_yolo_client_accepts_pil_image(tmp_path: Path) -> None:
    """Agent3 passes pre-decoded PIL pages; the client must not treat them
    as filesystem paths (regression: TypeError before any inference)."""

    model = tmp_path / "box.onnx"
    model.write_bytes(b"model")
    client = YoloClient(
        YoloDetectorRouter(
            {
                BOX_MODEL_TYPE: YoloModelConfig(
                    model,
                    confidence=0.2,
                )
            }
        )
    )
    image = Image.new("RGB", (64, 32), "white")

    result = client.detect(image)

    assert result.image == "<PIL Image 64x32>"
    assert result.detections[0].label == "INV"
    assert _FakeYOLO.calls[0][1]["source"] is image


def test_image_description_covers_all_source_types() -> None:
    from count_and_design.application.ai.yolo.client import (
        _image_description,
    )

    assert _image_description(b"raw") == "<bytes>"
    assert _image_description(Path("img.png")) == str(Path("img.png"))
    assert _image_description(Image.new("RGB", (10, 20))) == (
        "<PIL Image 10x20>"
    )
    assert _image_description(object()) == "<object>"


def test_yolo_router_rejects_unknown_model_type(tmp_path: Path) -> None:
    model = tmp_path / "tracker.onnx"
    model.write_bytes(b"tracker")
    router = YoloDetectorRouter({TRACKER_MODEL_TYPE: YoloModelConfig(model)})

    with pytest.raises(YoloDetectorError, match="Unknown YOLO model type: box"):
        router.detect(BOX_MODEL_TYPE, tmp_path / "page.png")


def test_default_yolo_configs_use_explicit_model_dir(tmp_path: Path) -> None:
    configs = default_yolo_configs(
        tmp_path,
        confidence=0.5,
        device="cpu",
    )
    router = create_yolo_router(tmp_path, confidence=0.5, device="cpu")

    assert configs[TRACKER_MODEL_TYPE].path == tmp_path / TRACKER_MODEL_FILENAME
    assert configs[BOX_MODEL_TYPE].path == tmp_path / BOX_MODEL_FILENAME
    assert configs[BOX_MODEL_TYPE].confidence == 0.5
    assert configs[TRACKER_MODEL_TYPE].iou == DEFAULT_TRACKER_NMS_IOU_THRESHOLD
    assert configs[BOX_MODEL_TYPE].iou == 0.7
    assert configs[BOX_MODEL_TYPE].device == "cpu"
    assert router.model_path_for(TRACKER_MODEL_TYPE) == tmp_path / TRACKER_MODEL_FILENAME
    assert router.model_path_for(BOX_MODEL_TYPE) == tmp_path / BOX_MODEL_FILENAME


def test_default_yolo_configs_allow_shared_iou_override(tmp_path: Path) -> None:
    configs = default_yolo_configs(tmp_path, iou=0.45)

    assert configs[TRACKER_MODEL_TYPE].iou == 0.45
    assert configs[BOX_MODEL_TYPE].iou == 0.45


def test_yolo_router_defaults_to_bundled_onnx_models() -> None:
    model_dir = bundled_yolo_model_dir()
    configs = default_yolo_configs()
    router = create_yolo_router()

    assert (model_dir / TRACKER_MODEL_FILENAME).exists()
    assert (model_dir / BOX_MODEL_FILENAME).exists()
    assert configs[TRACKER_MODEL_TYPE].path == model_dir / TRACKER_MODEL_FILENAME
    assert configs[BOX_MODEL_TYPE].path == model_dir / BOX_MODEL_FILENAME
    assert router.model_path_for(TRACKER_MODEL_TYPE) == model_dir / TRACKER_MODEL_FILENAME
    assert router.model_path_for(BOX_MODEL_TYPE) == model_dir / BOX_MODEL_FILENAME


def test_block_schedule_uses_bundled_yolov26s_model() -> None:
    from count_and_design.application.ai.yolo.runtime import (
        BLOCK_SCHEDULE_MODEL_FILENAME,
    )

    model_path = bundled_yolo_model_dir() / BLOCK_SCHEDULE_MODEL_FILENAME

    assert BLOCK_SCHEDULE_MODEL_FILENAME == "block_schedule_yolov26s.onnx"
    assert model_path.exists()

def test_default_configs_can_discover_renamed_onnx_models(tmp_path: Path) -> None:
    tracker_model = tmp_path / "renamed_tracker_model.onnx"
    box_model = tmp_path / "renamed_box_model.onnx"
    tracker_model.write_bytes(b"tracker")
    box_model.write_bytes(b"box")

    configs = default_yolo_configs(tmp_path)

    assert configs[TRACKER_MODEL_TYPE].path == tracker_model
    assert configs[BOX_MODEL_TYPE].path == box_model

def test_detect_yolo_direct_call_uses_selected_model(tmp_path: Path) -> None:
    tracker_model = tmp_path / TRACKER_MODEL_FILENAME
    box_model = tmp_path / BOX_MODEL_FILENAME
    image = tmp_path / "page.png"
    tracker_model.write_bytes(b"tracker")
    box_model.write_bytes(b"box")
    image.write_bytes(b"image")

    detections = detect_yolo(
        BOX_MODEL_TYPE,
        image,
        model_dir=tmp_path,
        confidence=0.44,
        device="cpu",
    )

    assert len(detections) == 1
    assert detections[0].label == "INV"
    assert _FakeYOLO.loaded_paths == [str(box_model)]
    assert _FakeYOLO.loaded_options[0]["task"] == "detect"
    assert _FakeYOLO.calls[0][1]["conf"] == 0.44
    assert _FakeYOLO.calls[0][1]["device"] == "cpu"


def test_detect_yolo_to_json_writes_detection_payload(tmp_path: Path) -> None:
    tracker_model = tmp_path / TRACKER_MODEL_FILENAME
    box_model = tmp_path / BOX_MODEL_FILENAME
    image = tmp_path / "page.png"
    output = tmp_path / "detections.json"
    tracker_model.write_bytes(b"tracker")
    box_model.write_bytes(b"box")
    image.write_bytes(b"image")

    payload = detect_yolo_to_json(
        TRACKER_MODEL_TYPE,
        image,
        output,
        model_dir=tmp_path,
        confidence=0.2,
        device="cpu",
    )
    written = json.loads(output.read_text(encoding="utf-8"))

    assert payload == written
    assert written["model_type"] == TRACKER_MODEL_TYPE
    assert written["model_path"] == str(tracker_model)
    assert written["image"] == str(image)
    assert written["confidence"] == 0.2
    assert written["device"] == "cpu"
    assert _FakeYOLO.calls[0][1]["iou"] == DEFAULT_TRACKER_NMS_IOU_THRESHOLD
    assert written["detections"][0]["label"] == "INV"
    assert written["detections"][0]["box"] == {
        "x1": 1.0,
        "y1": 2.0,
        "x2": 30.0,
        "y2": 40.0,
    }


def test_yolo_parallelism_uses_cpu_budget(monkeypatch) -> None:
    import count_and_design.application.ai.yolo.router as router_module

    monkeypatch.delenv("AGENT3_YOLO_WORKERS", raising=False)
    monkeypatch.setattr(router_module.os, "cpu_count", lambda: 20)
    router = YoloDetectorRouter(
        {BOX_MODEL_TYPE: YoloModelConfig(Path("box.onnx"))}
    )

    assert router.configure_parallelism(19) == (5, 4)

    monkeypatch.setenv("AGENT3_YOLO_WORKERS", "99")
    assert router.configure_parallelism(19) == (19, 1)


def test_onnx_detector_builds_configured_session_pool(tmp_path: Path, monkeypatch) -> None:
    model = tmp_path / "model.onnx"
    model.write_bytes(b"fake-model")
    created = []

    class _Options:
        intra_op_num_threads = 0
        inter_op_num_threads = 0
        execution_mode = None

    class _Session:
        def __init__(self, _path, *, sess_options, providers):
            self.options = sess_options
            self.providers = providers
            created.append(self)

        def get_inputs(self):
            return [SimpleNamespace(name="images", shape=[1, 3, 32, 32])]

        def get_modelmeta(self):
            return SimpleNamespace(custom_metadata_map={"names": "{0: 'Box'}"})

        def run(self, _outputs, _inputs):
            import numpy as np

            return [
                np.array(
                    [[[16.0], [16.0], [8.0], [8.0], [0.9]]],
                    dtype=np.float32,
                )
            ]

    fake_ort = ModuleType("onnxruntime")
    fake_ort.SessionOptions = _Options
    fake_ort.InferenceSession = _Session
    fake_ort.ExecutionMode = SimpleNamespace(ORT_SEQUENTIAL="sequential")
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)

    detector = OnnxRuntimeYoloDetector(model)
    detector.configure_parallelism(3, 4)
    detector.warmup()

    assert len(created) == 3
    assert all(item.options.intra_op_num_threads == 4 for item in created)
    assert all(item.options.inter_op_num_threads == 1 for item in created)
    assert all(item.providers == ["CPUExecutionProvider"] for item in created)

    detections = detector.detect(Image.new("RGB", (16, 8), "white"))
    assert len(detections) == 1
    assert detections[0].label == "Box"
    assert detections[0].confidence == pytest.approx(0.9)
    assert detections[0].box.to_tuple() == pytest.approx((6.0, 2.0, 10.0, 6.0))


def test_onnx_detector_maps_boxes_from_large_drawings(tmp_path: Path, monkeypatch) -> None:
    """6000px pages must keep original coordinates after cheap downscale."""

    model = tmp_path / "model.onnx"
    model.write_bytes(b"fake-model")

    class _Options:
        intra_op_num_threads = 0
        inter_op_num_threads = 0
        execution_mode = None

    class _Session:
        def __init__(self, _path, *, sess_options, providers):
            del _path, sess_options, providers

        def get_inputs(self):
            return [SimpleNamespace(name="images", shape=[1, 3, 32, 32])]

        def get_modelmeta(self):
            return SimpleNamespace(custom_metadata_map={"names": "{0: 'Box'}"})

        def run(self, _outputs, _inputs):
            import numpy as np

            return [
                np.array(
                    [[[16.0], [16.0], [8.0], [8.0], [0.9]]],
                    dtype=np.float32,
                )
            ]

    fake_ort = ModuleType("onnxruntime")
    fake_ort.SessionOptions = _Options
    fake_ort.InferenceSession = _Session
    fake_ort.ExecutionMode = SimpleNamespace(ORT_SEQUENTIAL="sequential")
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)

    detector = OnnxRuntimeYoloDetector(model)
    detections = detector.detect(Image.new("RGB", (3200, 1600), "white"))

    assert len(detections) == 1
    assert detections[0].box.to_tuple() == pytest.approx(
        (1200.0, 400.0, 2000.0, 1200.0)
    )
