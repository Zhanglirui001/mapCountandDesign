"""Canonical Agent2/Agent3 connection-orientation option descriptions.

Single wording source for BOTH GUI stages (info-recognition and
element-recognition): render option labels only through
:func:`panel_orientation_text` so the two stages can never drift.
"""

from __future__ import annotations


PANEL_ORIENTATION_OPTIONS = (
    ("P-B", "Positive to Box", "正极朝汇流箱 Box"),
    ("N-B", "Negative to Box", "负极朝汇流箱 Box"),
    ("P-N", "Positive to North", "正极朝北"),
    ("P-S", "Positive to South", "正极朝南"),
    ("P-E", "Positive to East", "正极朝东"),
    ("P-W", "Positive to West", "正极朝西"),
)

_PANEL_ORIENTATION_BY_CODE = {
    code: (english, chinese)
    for code, english, chinese in PANEL_ORIENTATION_OPTIONS
}


def panel_orientation_text(code: str) -> str:
    """Return the bilingual description originally defined by Agent2."""

    english, chinese = _PANEL_ORIENTATION_BY_CODE[code]
    return f"{english}  ·  {chinese}  ({code})"
