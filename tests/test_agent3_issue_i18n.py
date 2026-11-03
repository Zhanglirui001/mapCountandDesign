import json
import pickle
from pathlib import Path
from string import Formatter

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from count_and_design.application.ai_statistics.element_recognition.contracts import (
    ElementPageResult,
    ElementRecognitionResult,
    RecognitionIssue,
)
from count_and_design.application.ai_statistics.element_recognition.issues import (
    ISSUE_DEFINITIONS,
    IssueCode,
    create_issue,
    format_issue_message,
)
from count_and_design.application.ai_statistics.models import ReviewedTree
from count_and_design.gui.views.ai_statistics.pages.element_recognition import (
    ElementRecognitionPage,
)
from count_and_design.i18n import TranslationManager, tr


@pytest.mark.parametrize("locale", ["en_US", "zh_CN", "pl_PL"])
def test_every_issue_has_a_complete_formatable_translation(locale):
    directory = (
        Path(__file__).resolve().parents[1] / "src/count_and_design/i18n/translations"
    )
    translations = json.loads(
        (directory / f"{locale}.json").read_text(encoding="utf-8")
    )
    values = {
        "name": "CBX01",
        "candidates": "INV1, INV2",
        "inferred": "INV3",
        "expected_text": "<empty>",
        "tracker_id": "p1:tracker:1",
        "measured": 100.0,
        "deviation": 0.5,
        "strings": 2,
        "ocr_count": 5,
        "expected_length": 200.0,
        "inv_name": "INV1",
        "detail": 'provider returned {"error": "timeout"}',
        "node_id": "p1:node:1",
        "peers": "p1:node:2",
        "threshold": 0.3,
        "max_iou": 0.456,
        "pair_labels": "t1/t2",
        "target": "unassigned Tracker",
        "target_id": "p1:tracker:1",
    }
    for code, definition in ISSUE_DEFINITIONS.items():
        key = f"ai_statistics.element_recognition.issue.{code.value}"
        template = translations[key]
        fields = {
            name for _, name, _, _ in Formatter().parse(definition.template) if name
        }
        translated_fields = {
            name for _, name, _, _ in Formatter().parse(template) if name
        }
        assert fields == translated_fields
        issue = create_issue(code, **{name: values[name] for name in fields})
        formatted = format_issue_message(
            issue, lambda key, default: translations.get(key, default)
        )
        assert formatted
        if "detail" in fields:
            assert values["detail"] in formatted
            assert (
                formatted != values["detail"]
            )  # A translated operation label precedes raw details.
        if "max_iou" in fields:
            assert "45.6%" in formatted
        if code == IssueCode.OVERLAPPING_TRACKER_BOXES:
            assert (
                translations[
                    "ai_statistics.element_recognition.issue.value.unassigned_tracker"
                ]
                in formatted
            )


def test_issue_parameters_survive_process_transfer_and_json_export():
    issue = create_issue(IssueCode.NODE_HAS_NO_BOX, name="CBX01", page_number=1)
    transferred = pickle.loads(pickle.dumps(issue))
    assert transferred.params == {"name": "CBX01"}
    assert transferred.key == issue.key
    result = ElementRecognitionResult(ReviewedTree(), issues=(transferred,))
    exported = json.loads(json.dumps(result.to_dict()))["issues"][0]
    assert exported["params"] == {"name": "CBX01"}
    assert exported["message"] == issue.message


def test_legacy_unknown_and_broken_translations_fall_back_without_formatting_raw_details():
    legacy = RecognitionIssue("node_has_no_box", 'raw {"detail": "unknown"}')
    assert (
        format_issue_message(legacy, lambda *_: "Box missing for {name}")
        == legacy.message
    )
    assert format_issue_message(legacy, lambda *_: "bad {name") == legacy.message
    assert format_issue_message(legacy, lambda *_: "") == legacy.message
    unknown = RecognitionIssue("external_code", "Provider {detail}")
    assert format_issue_message(unknown, lambda *_: "") == unknown.message


def test_issue_language_switch_preserves_ignore_state_and_searches_translated_text():
    app = QApplication.instance() or QApplication([])
    manager = TranslationManager.instance()
    original_locale = manager.locale
    page = ElementRecognitionPage()
    try:
        manager.locale = "en_US"
        issue = create_issue(
            IssueCode.NODE_HAS_NO_BOX, name="CBX01", page_number=1, entity_id="n1"
        )
        page.set_result(
            ElementRecognitionResult(
                ReviewedTree(),
                pages=(ElementPageResult(1, "INV1", 100, 100, issues=(issue,)),),
            )
        )
        assert "No Box was assigned" in page.issues.item(0).text()
        page._set_issue_muted(issue.key, muted=True)
        manager.locale = "zh_CN"
        page.retranslate()
        app.processEvents()
        assert "未分配到 Box" in page.issues.item(0).text()
        assert page.issues.item(0).data(Qt.ItemDataRole.UserRole + 4) is True
        assert issue.key in page._muted_issue_keys
        assert issue.message == "No Box was assigned to CBX01."
        page.issue_search.setText("未分配")
        page._apply_issue_search()
        assert page.issues.count() == 1
        page.issue_search.clear()
        manager.locale = "pl_PL"
        page.retranslate()
        assert "nie przypisano" in page.issues.item(0).text()
        assert "CBX01" in format_issue_message(issue, tr)
    finally:
        page.close()
        manager.locale = original_locale
