"""Whole-page review for Agent3 Node names.

The crop reader is intentionally kept separate from this pass: one model reads
each detected Node cheaply and concurrently, then the review model sees the
original Array Layout page plus the complete page-level context in one
request.  The stable integer ``number`` is the join key between a recognized
name, its YOLO bounding box, and the reviewer's corrected output.

The review model is Bedrock Claude by default;
``AGENT3_NODE_REVIEW_LLM_PROVIDER`` selects another provider (for example
``openrouter``).

Node names stay copies of schedule-table rows. The page's INV name is
different: Agent2's list authorizes which INV the page belongs to, while
the reported spelling is the most complete one the page prints, so a page
showing ``INV1201`` for the listed ``1201`` keeps that fuller name.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

from PIL import Image

from ....ai import BoundingBox
from ....ai.llm import (
    BedrockLlmProvider,
    OpenRouterLlmProvider,
    create_llm_provider_from_env,
)
from ....env import env_str
from ....image_transport import ImageTransportError, compress_image_for_bedrock
from ....ordering import natural_text_key
from ..contracts import NodeStringTableRow
from .recognizer import (
    _equipment_signature,
    _normalize_name,
    _signature_matches,
)


NODE_NAME_REVIEW_SYSTEM_PROMPT = (
    "You strictly audit OCR equipment names on solar engineering Array Layout "
    "drawings. Every final Node name must be an exact row of the supplied "
    "schedule table; route/connection annotations and other drawing notes are "
    "never Node names. The Agent2 INV-name list decides which INV the page "
    "belongs to, while the returned INV name keeps the most complete spelling "
    "the page prints for it. Use the image as visual evidence and return only "
    "the requested JSON."
)


class NodeNameReviewError(RuntimeError):
    """Raised when Claude review or its structured response is unusable."""

    def __init__(
        self,
        message: str,
        *,
        raw_llm: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_llm = raw_llm


@dataclass(frozen=True)
class NodeNameReviewCandidate:
    """One numbered Node name and its full-page location."""

    number: int
    recognized_name: str
    bbox: BoundingBox
    raw_ocr_text: str = ""


@dataclass(frozen=True)
class NodeNameReviewResult:
    """Validated incremental corrections returned by Claude."""

    corrections_by_number: dict[int, str]
    inv_name: str | None
    all_correct: bool
    prompt: str = field(repr=False, compare=False)
    raw_output: dict[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )


class NodeNameReviewer:
    """Review all detected Node names on one original Array Layout page."""

    def __init__(
        self,
        bedrock: BedrockLlmProvider | OpenRouterLlmProvider | None = None,
        *,
        max_transport_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        # ``bedrock`` keeps its historical name for injected providers; the
        # Agent3 review provider is created on first use when none is injected.
        self._bedrock = bedrock
        self._max_transport_bytes = max_transport_bytes

    def review(
        self,
        image_bytes: bytes,
        table_rows: Sequence[NodeStringTableRow],
        candidates: Sequence[NodeNameReviewCandidate],
        expected_inv_names: Sequence[str] = (),
    ) -> NodeNameReviewResult:
        if not image_bytes:
            raise NodeNameReviewError("Array Layout image bytes are empty.")
        if not candidates:
            return NodeNameReviewResult({}, None, True, "", {})

        try:
            with Image.open(BytesIO(image_bytes)) as image:
                width, height = image.size
        except Exception as exc:  # noqa: BLE001 - normalized domain error
            raise NodeNameReviewError(
                f"Array Layout image could not be decoded: {exc}"
            ) from exc

        prompt = build_node_name_review_prompt(
            table_rows,
            candidates,
            expected_inv_names=expected_inv_names,
            image_width=width,
            image_height=height,
        )
        try:
            transport_image, mime_type = compress_image_for_bedrock(
                image_bytes,
                max_transport_bytes=self._max_transport_bytes,
            )
        except ImageTransportError as exc:
            raise NodeNameReviewError(
                f"Array Layout image could not be prepared for review: {exc}",
                raw_llm={"prompt": prompt},
            ) from exc

        try:
            llm = self._bedrock
            if llm is None:
                llm = create_llm_provider_from_env(
                    provider=env_str(
                        "AGENT3_NODE_REVIEW_LLM_PROVIDER",
                        "bedrock",
                    )
                )
                self._bedrock = llm
            completion = llm.complete_with_image(
                prompt,
                transport_image,
                mime_type=mime_type,
                system_prompt=NODE_NAME_REVIEW_SYSTEM_PROMPT,
                # A reasoning model spends part of this budget before the
                # JSON answer, so keep real headroom over corrections * 64.
                max_completion_tokens=min(
                    12_000,
                    max(2048, len(candidates) * 256),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - normalize provider failures
            raise NodeNameReviewError(
                f"Node-name review failed: {exc}",
                raw_llm={"prompt": prompt},
            ) from exc

        raw_output = _llm_raw_output(completion)
        try:
            parsed = parse_node_name_review_result(
                str(getattr(completion, "content", "")),
                expected_numbers=tuple(item.number for item in candidates),
                current_names_by_number={
                    item.number: item.recognized_name for item in candidates
                },
                expected_inv_names=expected_inv_names,
            )
        except NodeNameReviewError as exc:
            raise NodeNameReviewError(
                str(exc),
                raw_llm={**raw_output, "prompt": prompt},
            ) from exc
        return NodeNameReviewResult(
            parsed.corrections_by_number,
            parsed.inv_name,
            parsed.all_correct,
            prompt,
            raw_output,
        )


@dataclass(frozen=True)
class ParsedNodeNameReview:
    """Structured meaning of either the full or compact Claude response."""

    corrections_by_number: dict[int, str]
    inv_name: str | None = None
    all_correct: bool = False


def build_node_name_review_prompt(
    table_rows: Sequence[NodeStringTableRow],
    candidates: Sequence[NodeNameReviewCandidate],
    *,
    expected_inv_names: Sequence[str] = (),
    image_width: int,
    image_height: int,
) -> str:
    """Build the single multimodal request used for one Array Layout page."""

    # Schedule OCR order is not always reliable. Present rows in natural Node
    # order (…-2 before …-10) so missing sequence values stand out to Claude.
    sorted_table_rows = sorted(
        table_rows,
        key=lambda row: natural_text_key(row.node),
    )
    table_payload = [
        {"node": row.node, "string_number": row.string_number}
        for row in sorted_table_rows
    ]
    node_payload = []
    safe_width = max(1, image_width)
    safe_height = max(1, image_height)
    for item in candidates:
        box = item.bbox
        node_payload.append(
            {
                "number": item.number,
                "recognized_name": item.recognized_name,
                "raw_ocr_text": item.raw_ocr_text,
                "bbox_pixels": {
                    "x1": box.x1,
                    "y1": box.y1,
                    "x2": box.x2,
                    "y2": box.y2,
                },
                "bbox_normalized": {
                    "x1": round(box.x1 / safe_width, 6),
                    "y1": round(box.y1 / safe_height, 6),
                    "x2": round(box.x2 / safe_width, 6),
                    "y2": round(box.y2 / safe_height, 6),
                },
            }
        )

    unclaimed_rows = _unclaimed_table_rows(sorted_table_rows, candidates)

    return (
        "Audit the preliminary OCR Node names below against the attached "
        "original Array Layout drawing. Each Node's recognized name and "
        "bounding box share the same integer number; that number is the "
        "immutable identity and must never change, merge, or disappear.\n\n"
        "WHAT A NODE NAME IS\n"
        "- A Node is one combiner box / load-break disconnect symbol on this "
        "page.\n"
        "- Its name is a row of the page schedule table. Copy the matching "
        "row exactly: same INV spelling, same separators, same digits "
        "including leading zeros.\n"
        "- All Node names on one page share one format (token order, "
        "separators, prefix spelling, digit style). The table rows and the "
        "names that already match them show that format; never force a name "
        "into a template assumed from another drawing.\n"
        "- The schedule table is the only authority for Node names. Every "
        "correction must be copied from a table row; the preliminary text "
        "itself is never authoritative.\n\n"
        "WHAT IS NOT A NODE NAME\n"
        "- Route, jumper, and connection annotations that join two equipment "
        "IDs, however the connector is written, for example "
        '"CBX13-to-CBX14", "CBX-13/CBX-14" or "CBX13->CBX14". These are '
        "wire labels, not equipment names; a Node whose preliminary name "
        "has this shape is always wrong. Such an annotation can sit inside "
        "or next to a Node's bounding box without being its name.\n"
        "- Cable specifications, dimensions, notes, titles, revision marks, "
        "legend text, and other drawing annotations are never Node names.\n\n"
        "PAGE INV NAME (inv_name)\n"
        "- The Agent2 INV-name list decides which INV this page belongs to, "
        "not which characters you report. After ignoring letter case, "
        "separators and a leading INV token, the returned name must resolve "
        "to exactly one list entry: INV1201, INV 1201 and 1201 are the same "
        "INV.\n"
        "- Drawings and table headers often print the INV more completely "
        "than the list does (list 1201, page INV1201). Return the most "
        "complete spelling this page shows, keeping its INV token, "
        "separators, digits and leading zeros. Never shorten it to the "
        "list's spelling, and never add a character the page does not show; "
        "when the page shows nothing beyond the list entry, that entry's "
        "spelling stands.\n"
        "- This rule governs inv_name only. Node names keep copying their "
        "schedule table row exactly, even when the row or the list prints a "
        "shorter INV than the page title.\n\n"
        "REVIEW PROCEDURE (follow in order)\n"
        "1. Identify the one INV from the Agent2 INV-name list that this "
        "page belongs to and return it as inv_name by the rules above "
        "(null when the list is empty or no listed INV matches the "
        "drawing).\n"
        "2. Infer the page's naming format from the schedule table and the "
        "names that already match it, then match Nodes to table rows. A "
        "Node whose preliminary name is an exact table row is settled; a "
        "preliminary name that departs from the shared format is wrong. "
        "Pair the remaining Nodes with the remaining table rows using "
        "sequence numbers, bounding-box position, the numbers visible "
        "inside an annotation, and the data below.\n"
        "3. Emit a correction for every settled Node whose preliminary name "
        "is not its table row, and for every Node matched to an unmatched "
        "table row. OCR typos, wrong sequence digits, a wrong or missing "
        "INV identifier, a deviating format, and non-Node annotations all "
        "require a correction.\n"
        "4. Verify the final names: each correction must be an exact table "
        "row (or, with an empty table, follow the shared format shown by "
        "the other Node names), must be consistent with that shared format, "
        "and no two Nodes may end with the same name.\n"
        "5. Never invent a name. If a Node truly has no table row to match, "
        "leave its preliminary name unchanged; the pipeline reports it for "
        "manual review.\n\n"
        "OUTPUT CONTRACT\n"
        "Return only JSON, listing only the Nodes whose name changes. Each "
        "number appears at most once; unlisted Nodes keep their preliminary "
        "name:\n"
        '{"inv_name":"<matching INV as this page prints it>",'
        '"corrections":[{"number":1,"name":"exact table spelling"}]}\n'
        "If and only if no Node needs a correction, return "
        '{"inv_name":"<matching INV as this page prints it>",'
        '"corrections":"All correct"}.\n'
        "When the schedule table below is empty, judge names by the format "
        "shared by the other Node names and by the page INV spelling "
        "instead.\n\n"
        f"Original image dimensions: {image_width} x {image_height} pixels.\n"
        "Agent2 INV-name JSON:\n"
        + json.dumps(list(expected_inv_names), ensure_ascii=False, separators=(",", ":"))
        + "\nTable Node-name/string-count JSON:\n"
        + json.dumps(table_payload, ensure_ascii=False, separators=(",", ":"))
        + "\nTable rows unmatched by any preliminary Node name:\n"
        + json.dumps(unclaimed_rows, ensure_ascii=False, separators=(",", ":"))
        + "\nNumbered recognized Nodes and bounding boxes:\n"
        + json.dumps(node_payload, ensure_ascii=False, separators=(",", ":"))
    )


def _unclaimed_table_rows(
    table_rows: Sequence[NodeStringTableRow],
    candidates: Sequence[NodeNameReviewCandidate],
) -> list[str]:
    """Return table row names that no preliminary Node name accounts for.

    Exact spelling is tried first, then the order-insensitive equipment
    signature used by crop matching, so a row stays claimed when its Node
    carries a harmless OCR typo. The remaining rows are the ones whose
    Nodes were read wrongly (including connection annotations mistaken for
    a name) or were not detected at all; sending them lets Claude pair the
    remaining Nodes directly instead of diffing two lists on its own.
    """

    claimed: set[str] = set()
    for item in candidates:
        name = item.recognized_name.strip()
        if not name:
            continue
        normalized = _normalize_name(name)
        exact = [
            row.node
            for row in table_rows
            if row.node not in claimed
            and _normalize_name(row.node) == normalized
        ]
        if len(exact) == 1:
            claimed.add(exact[0])
            continue
        signature = _equipment_signature(name)
        if signature is None:
            continue
        tolerant = [
            row.node
            for row in table_rows
            if row.node not in claimed
            and _signature_matches(
                signature,
                _equipment_signature(row.node),
                fuzzy_id=True,
            )
        ]
        if len(tolerant) == 1:
            claimed.add(tolerant[0])
    return [row.node for row in table_rows if row.node not in claimed]


def parse_node_name_review_response(
    content: str,
    *,
    expected_numbers: Sequence[int],
) -> dict[int, str]:
    """Parse and return only the number-to-name corrections from Claude."""

    return parse_node_name_review_result(
        content,
        expected_numbers=expected_numbers,
    ).corrections_by_number


def parse_node_name_review_result(
    content: str,
    *,
    expected_numbers: Sequence[int],
    current_names_by_number: Mapping[int, str] | None = None,
    expected_inv_names: Sequence[str] = (),
) -> ParsedNodeNameReview:
    """Parse Claude's full mapping or its token-saving ``All correct`` marker."""

    # Retained in the signature for source compatibility with the first
    # revision of this API. Incremental correction parsing no longer needs to
    # receive or echo the existing names.
    del current_names_by_number
    if _is_all_correct_marker(content):
        return ParsedNodeNameReview({}, all_correct=True)

    payload = _first_json_value(content)
    if not isinstance(payload, Mapping):
        raise NodeNameReviewError(
            "Claude Node-name review response was not a JSON object."
        )
    inv_name = _parse_reviewed_inv_name(
        payload.get("inv_name"),
        expected_inv_names,
    )
    rows = payload.get("corrections", payload.get("node_names"))
    if isinstance(rows, str) and _is_all_correct_marker(rows):
        return ParsedNodeNameReview(
            {},
            inv_name=inv_name,
            all_correct=True,
        )
    if not isinstance(rows, list):
        raise NodeNameReviewError(
            "Claude Node-name review JSON must contain a corrections list or All correct."
        )

    result: dict[int, str] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise NodeNameReviewError(
                f"Claude corrections[{index}] must be an object."
            )
        number = row.get("number")
        name = row.get("name")
        if isinstance(number, bool) or not isinstance(number, int):
            raise NodeNameReviewError(
                f"Claude corrections[{index}].number must be an integer."
            )
        if number in result:
            raise NodeNameReviewError(
                f"Claude returned Node number {number} more than once."
            )
        if not isinstance(name, str) or not name.strip():
            raise NodeNameReviewError(
                f"Claude returned an empty name for Node number {number}."
            )
        result[number] = name.strip()

    unexpected = sorted(set(result) - set(expected_numbers))
    if unexpected:
        raise NodeNameReviewError(
            "Claude Node-name review returned unexpected Node numbers: "
            f"{unexpected}"
        )
    return ParsedNodeNameReview(
        result,
        inv_name=inv_name,
        all_correct=not result,
    )


def _parse_reviewed_inv_name(
    value: object,
    expected_inv_names: Sequence[str],
) -> str | None:
    """Return the page spelling of one listed INV, or None for fallback.

    The Agent2 list authorizes which INV the page belongs to; it does not
    dictate the reported characters. A drawing that prints ``INV1201`` for
    the listed ``1201`` therefore keeps its fuller spelling, while a name
    that merely differs in case or separators is reported with the list's
    own formatting. Nothing that leaves the list is accepted.
    """

    if not isinstance(value, str) or not value.strip():
        return None
    returned = value.strip()
    key = _name_key(returned)
    exact = [name for name in expected_inv_names if _name_key(name) == key]
    if exact:
        return exact[0] if len(exact) == 1 else None
    identity = _inv_identity(returned)
    if not identity:
        return None
    matches = [
        name
        for name in expected_inv_names
        if _inv_identity(name) == identity
    ]
    if len(matches) != 1:
        return None
    listed = matches[0]
    return returned if len(key) > len(_name_key(listed)) else listed


def _name_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _inv_identity(value: str) -> str:
    """Characters that identify an INV regardless of its optional INV token.

    Mirrors Agent2's own INV identity: ``1201``, ``INV 1201`` and
    ``INV1201`` name the same INV, so a page may print the fuller spelling
    without leaving the authoritative list.
    """

    key = _name_key(value)
    return key[3:] if key.startswith("INV") and len(key) > 3 else key


def _is_all_correct_marker(content: str) -> bool:
    text = str(content).strip()
    fenced = re.fullmatch(
        r"```(?:text|json)?\s*(.*?)\s*```",
        text,
        re.S | re.I,
    )
    if fenced:
        text = fenced.group(1).strip()
    return text.strip().strip('"\'').rstrip("。.!！") == "All correct"


def _first_json_value(content: str) -> Any | None:
    text = str(content).strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*([\[{].*[\]}])\s*```", text, re.S | re.I)
    if fenced:
        text = fenced.group(1)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", text):
        try:
            value, _end = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        return value
    return None


def _llm_raw_output(completion: object) -> dict[str, Any]:
    output: dict[str, Any] = {}
    content = getattr(completion, "content", "")
    if content:
        output["content"] = str(content)
    for name in ("model", "finish_reason", "reasoning"):
        value = getattr(completion, name, "")
        if value:
            output[name] = value
    usage = getattr(completion, "usage", None)
    if isinstance(usage, Mapping) and usage:
        output["usage"] = dict(usage)
    raw = getattr(completion, "raw", None)
    if isinstance(raw, Mapping) and raw:
        output["raw"] = dict(raw)
    return output
