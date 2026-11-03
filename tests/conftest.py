from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "test_scripts"))

from count_and_design.application.ai_statistics import (
    NumStrDetail,
    OverallSiteExtraction,
    TrackerFieldValue,
    TrackerOcrFields,
    build_agent4_validation_input,
    reviewed_tree_to_validation_payload,
)
from count_and_design.application.ai_statistics.models import (
    InvDraft,
    NodeDraft,
    OcrField,
    RackingEntryDraft,
    ReviewedTree,
    RowDraft,
    TrackerQuantity,
)


def _sample_row() -> RowDraft:
    return RowDraft(
        name="Row1",
        racking_entries=(
            RackingEntryDraft(rackings="3N", length_str="12str"),
            RackingEntryDraft(rackings="3S", length_str="1str_a"),
        ),
    )


def _sample_node(
    name: str,
    *,
    ocr_string_count: int,
    tracker_counts: tuple[tuple[str, int], ...],
    has_ocr_string_count: bool = True,
) -> NodeDraft:
    return NodeDraft(
        name=name,
        ocr_string_count=ocr_string_count,
        has_ocr_string_count=has_ocr_string_count,
        tracker_counts=tuple(
            TrackerQuantity(tracker_type=tracker_type, tracker_count=count)
            for tracker_type, count in tracker_counts
        ),
        rows=(_sample_row(),),
    )


def sample_agent3_reviewed_tree() -> ReviewedTree:
    return ReviewedTree(
        project_type="LYNX",
        length_identifiers=(
            OcrField(name="str", value="Fs6a"),
            OcrField(name="str_a", value="BYD"),
        ),
        invs=(
            InvDraft(
                name="INV1",
                page=12,
                nodes=(
                    _sample_node(
                        "Node1",
                        ocr_string_count=74,
                        tracker_counts=(("9-string", 5), ("13-string", 2)),
                    ),
                    _sample_node(
                        "Node2",
                        ocr_string_count=54,
                        tracker_counts=(("9-string", 6), ("13-string", 0)),
                    ),
                ),
            ),
            InvDraft(
                name="INV2",
                page=13,
                nodes=(
                    _sample_node(
                        "Node1",
                        ocr_string_count=72,
                        tracker_counts=(("9-string", 8), ("13-string", 0)),
                    ),
                    _sample_node(
                        "Node2",
                        ocr_string_count=247,
                        tracker_counts=(("9-string", 0), ("13-string", 19)),
                    ),
                ),
            ),
        ),
    )


def sample_agent2_extraction() -> OverallSiteExtraction:
    def field(label: str, value: str) -> TrackerFieldValue:
        return TrackerFieldValue(label=label, meaning="", value=value)

    return OverallSiteExtraction(
        page_number=1,
        tracker_ocr_fields=(
            TrackerOcrFields(
                module_model=field("MODULE MODEL", "Panel"),
                module_stc_dc_rating_w=field("POWER", "605W"),
                string_count=field("STRING COUNT", "0"),
                num_str=field("Num-STR", "9-STR, 13-STR"),
                num_str_details=(
                    NumStrDetail("9-STR", "19"),
                    NumStrDetail("13-STR", "21"),
                ),
            ),
        ),
    )


def sample_agent2_validation_payload() -> dict[str, object]:
    return build_agent4_validation_input((sample_agent2_extraction(),))


def sample_agent3_validation_payload() -> dict[str, object]:
    return reviewed_tree_to_validation_payload(sample_agent3_reviewed_tree())
