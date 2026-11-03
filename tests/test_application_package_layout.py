from count_and_design.application.catalog import (
    ProductPresetService,
    PVPanelService,
    RackingsService,
)
from count_and_design.application.core import ProjectPersistenceService
from count_and_design.application.data_fill import DataFillService, DataFillWorkflow
from count_and_design.application.project import (
    MergeService,
    ProjectStatsService,
    ProjectTreeService,
    ValidationService,
)
from count_and_design.application.ai.ocr.screenshot import (
    DeepSeekOcrService as ScreenshotDeepSeekOcrService,
)


def test_application_packages_expose_domain_entrypoints() -> None:
    assert DataFillService is not None
    assert DataFillWorkflow is not None
    assert ProjectPersistenceService is not None
    assert MergeService is not None
    assert ProjectStatsService is not None
    assert ProjectTreeService is not None
    assert ValidationService is not None
    assert ProductPresetService is not None
    assert PVPanelService is not None
    assert RackingsService is not None


def test_application_ai_ocr_exposes_screenshot_provider() -> None:
    assert ScreenshotDeepSeekOcrService is not None
