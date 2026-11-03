"""Lock the orientation option wording shared by both GUI stages.

Agent2 (info recognition) and Agent3 (element recognition) must render the
same bilingual option labels from ``orientation_options``.  Every Agent3
combo (project settings, property panel, per-INV mapping rows) goes through
``ElementRecognitionPage._populate_orientation_combo``, so asserting the
project and property combos covers all of them.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from count_and_design.application.ai_statistics import (
    PanelOrientation,
)
from count_and_design.gui.views.ai_statistics.pages.element_recognition import (
    ElementRecognitionPage,
)
from count_and_design.gui.views.ai_statistics.pages.info_recognition import (
    InfoRecognitionPage,
)
from count_and_design.gui.views.ai_statistics.pages.orientation_options import (
    PANEL_ORIENTATION_OPTIONS,
    panel_orientation_text,
)

_AGENT_CODES = [code for code, _english, _chinese in PANEL_ORIENTATION_OPTIONS]


def test_agent3_contract_understands_every_agent2_code() -> None:
    """The shared codes must keep round-tripping into Agent3's enum."""

    for code in _AGENT_CODES:
        assert PanelOrientation.from_value(code).value == code


def test_both_stages_render_identical_orientation_option_wording() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None
    expected = [panel_orientation_text(code) for code in _AGENT_CODES]

    info_page = InfoRecognitionPage()
    agent2 = info_page.orientation_combo
    # Index 0 is Agent2's required-selection placeholder (data=None).
    assert agent2.count() == len(_AGENT_CODES) + 1
    assert [agent2.itemData(index) for index in range(1, agent2.count())] == (
        _AGENT_CODES
    )
    assert [agent2.itemText(index) for index in range(1, agent2.count())] == (
        expected
    )

    element_page = ElementRecognitionPage()
    # Index 0 stays Agent3's internal UNKNOWN sentinel (data=UNKNOWN); the
    # four real options must match Agent2's wording one-for-one.
    for combo in (
        element_page.project_orientation,
        element_page.orientation_combo,
    ):
        assert combo.count() == len(_AGENT_CODES) + 1
        assert [
            combo.itemData(index).value for index in range(1, combo.count())
        ] == _AGENT_CODES
        assert [
            combo.itemText(index) for index in range(1, combo.count())
        ] == expected

    # Cross-stage guarantee: identical text for identical codes, always from
    # the single shared formatter.
    agent2_texts = [
        agent2.itemText(agent2.findData(code)) for code in _AGENT_CODES
    ]
    project_combo = element_page.project_orientation
    agent3_texts = [
        project_combo.itemText(
            project_combo.findData(PanelOrientation.from_value(code))
        )
        for code in _AGENT_CODES
    ]
    assert agent3_texts == agent2_texts == expected
