from __future__ import annotations

from dataclasses import replace

from count_and_design.application.ai_statistics import (
    AIStatisticsWorkflow,
    ElementRecognitionResult,
    FakePageClassificationProvider,
    InvDraft,
    NodeDraft,
    OverallSiteExtraction,
    ReviewedTree,
    inv_name_similarity,
    match_inv_names,
)


def test_fuzzy_matching_retains_both_original_recognition_names() -> None:
    matches = match_inv_names(
        ("1201", "West inverter abbreviated"),
        ("INV1201", "West inv abridged"),
    )

    assert [(item.agent2_name, item.agent3_name) for item in matches] == [
        ("1201", "INV1201"),
        ("West inverter abbreviated", "West inv abridged"),
    ]
    assert all(0.0 < item.similarity <= 1.0 for item in matches)


def test_matching_is_one_to_one_and_takes_strongest_pairs_first() -> None:
    matches = match_inv_names(
        ("INV1201", "INV1202"),
        ("1202", "INV-1201"),
    )

    assert {(item.agent2_name, item.agent3_name) for item in matches} == {
        ("INV1201", "INV-1201"),
        ("INV1202", "1202"),
    }
    assert inv_name_similarity("INV1201", "INV-1201") == 1.0


def test_names_without_any_shared_characters_remain_unmatched() -> None:
    assert match_inv_names(("ABC",), ("123",)) == ()


def test_result_serializes_background_matches_without_renaming_tree() -> None:
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV1201", nodes=(NodeDraft(name="N1"),)),)
        ),
        inv_name_matches=match_inv_names(("1201",), ("INV1201",)),
    )

    payload = result.to_dict()

    assert payload["reviewed_tree"]["invs"][0]["name"] == "INV1201"
    assert payload["inv_name_matches"][0]["agent2_name"] == "1201"
    assert payload["inv_name_matches"][0]["agent3_name"] == "INV1201"


def test_workflow_refreshes_matches_after_agent3_name_review() -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.complete_overall_site_recognition(
        (OverallSiteExtraction(page_number=1, inv_list=("1201",)),)
    )
    original = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV1201", nodes=(NodeDraft(name="N1"),)),)
        )
    )
    workflow.complete_element_recognition(original)

    edited_tree = replace(
        original.reviewed_tree,
        invs=(replace(original.reviewed_tree.invs[0], name="Array-1201"),),
    )
    snapshot = workflow.replace_element_review(edited_tree)

    assert snapshot.reviewed_tree is not None
    assert snapshot.reviewed_tree.invs[0].name == "Array-1201"
    assert snapshot.element_recognition_result is not None
    match = snapshot.element_recognition_result.inv_name_matches[0]
    assert (match.agent2_name, match.agent3_name) == ("1201", "Array-1201")


def test_workflow_refreshes_matches_after_agent2_name_review() -> None:
    workflow = AIStatisticsWorkflow(FakePageClassificationProvider())
    workflow.complete_overall_site_recognition(
        (OverallSiteExtraction(page_number=1, inv_list=("1201",)),)
    )
    result = ElementRecognitionResult(
        reviewed_tree=ReviewedTree(
            invs=(InvDraft(name="INV1201", nodes=(NodeDraft(name="N1"),)),)
        )
    )
    workflow.complete_element_recognition(result)

    snapshot = workflow.update_overall_site_review(
        (OverallSiteExtraction(page_number=1, inv_list=("Overall-1201",)),),
        "P-N",
    )

    assert workflow.agent2_inv_names == ("Overall-1201",)
    assert snapshot.element_recognition_result is not None
    match = snapshot.element_recognition_result.inv_name_matches[0]
    assert (match.agent2_name, match.agent3_name) == (
        "Overall-1201",
        "INV1201",
    )
