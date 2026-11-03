"""Node label (name) recognition for Agent3 element recognition.

This module owns the full Node-name pipeline that used to be scattered
through :mod:`..service`:

- Crop reading: one original-orientation Node crop is read with the vision
  provider selected by ``LLM_PROVIDER`` (LAN Qwen by default, an OpenRouter
  model when set to ``openrouter``), or with an injected MinerU OCR service
  for deterministic tests.
- Reading selection: confidence/text-quality voting across the recorded
  orientation readings.
- No match: a crop with no name tied to the page table is answered with an
  explicit ``No Match`` marker, which becomes an empty reading so the caller
  keeps its numbered placeholder instead of inventing a name.
- Name matching: normalization, order-insensitive equipment signatures
  and schedule-table matching used to canonicalize a raw OCR reading.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from concurrent.futures import Future
from io import BytesIO
from statistics import mean
from threading import Lock

from PIL import Image

from ....ai import BoundingBox
from ....ai.llm import (
    OpenRouterLlmProvider,
    QwenLlmProvider,
    create_llm_provider_from_env,
)

from ..contracts import NodeStringTableRow

# Equipment-name components used for order-insensitive matching. CAD labels
# and schedule tables frequently print the same name with opposite token
# order ("-CBX- INV11A101 01" vs "INV11A101-CBX-01").
_INV_ID_PATTERN = re.compile(r"[0-9]{1,6}[A-Z][0-9]{1,6}")
_NUMBER_PATTERN = re.compile(r"[0-9]{1,6}")
_EQUIPMENT_TOKEN_PATTERN = re.compile(r"\b(CBX|LBD|LYNX)\b")

# Keys a MinerU payload may use to expose a per-request confidence. Current
# deployments return none of these; the lookup exists so a future field is
# honored automatically without changing orientation voting.
_CONFIDENCE_KEYS = frozenset(
    {
        "confidence",
        "ocr_confidence",
        "text_confidence",
        "avg_logprob",
        "mean_logprob",
    }
)

# Claude receives the crop in its original drawing orientation only. Rotating
# a crop can change the visual reading order and was explicitly removed from
# the Agent3 contract.
_NODE_NAME_SYSTEM_PROMPT = (
    "You read equipment labels from engineering drawings. "
    "Return only the Node name, or the literal No Match when the crop shows "
    "no name, with no explanation."
)

# A crop whose label cannot be tied to a listed Node name gets an explicit
# no-match answer instead of a guessed name. The pipeline turns that answer
# into an empty reading, so these spellings all mean "no name".
_NO_MATCH_MARKER = "No Match"
_NO_MATCH_MARKERS = frozenset(
    {
        "no match",
        "no matches",
        "nomatch",
        "none",
        "null",
        "n/a",
        "unknown",
        "无匹配",
        "未匹配",
    }
)


class NodeNameRecognizer:
    """Read one Node label crop via injected MinerU OCR or the vision LLM.

    The production provider is created lazily: ``qwen`` stays injectable
    (and falsy) until the first real crop is read, so tests can observe an
    uninitialized recognizer without touching network configuration.
    """

    def __init__(
        self,
        node_ocr: object | None = None,
        qwen: QwenLlmProvider | OpenRouterLlmProvider | None = None,
    ) -> None:
        self.node_ocr = node_ocr
        self.qwen = qwen
        self._qwen_lock = Lock()

    def read_crop(
        self,
        decoded: Image.Image,
        bbox: BoundingBox,
        page_number: int,
        index: int,
        table_rows: tuple[NodeStringTableRow, ...] = (),
    ) -> tuple[str, float | None]:
        """Read one original-orientation Node crop with the configured provider.

        Returns ``(text, confidence)``. Confidence is ``None`` unless the
        OCR payload exposes a usable confidence field.
        """
        return self.read_image(
            self.encode_crop(decoded, bbox), page_number, index, table_rows
        )

    @staticmethod
    def encode_crop(decoded: Image.Image, bbox: BoundingBox) -> bytes:
        """Detach a compressed crop so waiting requests retain no page bitmap."""
        pad = max(2, int(min(decoded.size) * 0.002))
        # Detections may overshoot the canvas by a pixel or two; clamp each
        # edge independently so degenerate boxes never invert the crop rect.
        left = min(max(0, int(bbox.x1) - pad), decoded.width)
        top = min(max(0, int(bbox.y1) - pad), decoded.height)
        right = min(max(0, int(bbox.x2) + pad), decoded.width)
        bottom = min(max(0, int(bbox.y2) + pad), decoded.height)
        if right <= left or bottom <= top:
            return b""
        with decoded.crop((left, top, right, bottom)) as crop:
            output = BytesIO()
            crop.save(output, format="PNG")
            return output.getvalue()

    def read_image(
        self,
        image_bytes: bytes,
        page_number: int,
        index: int,
        table_rows: tuple[NodeStringTableRow, ...] = (),
    ) -> tuple[str, float | None]:
        if not image_bytes:
            return "", None
        if self.node_ocr is not None:
            parsed = self.node_ocr.parse_file(
                image_bytes,
                filename=f"array-page-{page_number}-node-{index}.png",
                parse_method="ocr",
                table_enable=False,
                formula_enable=False,
                image_analysis=False,
            )
            return parsed.markdown.strip(), _service_ocr_confidence(parsed)

        llm = self.qwen
        if llm is None:
            with self._qwen_lock:
                llm = self.qwen
                if llm is None:
                    llm = create_llm_provider_from_env()
                    self.qwen = llm
        completion = llm.complete_with_image(
            _node_name_prompt(table_rows),
            image_bytes,
            mime_type="image/png",
            system_prompt=_NODE_NAME_SYSTEM_PROMPT,
        )
        return _clean_node_name_completion(completion.content), None


def _service_ocr_confidence(parsed: object) -> float | None:
    """Extract an OCR-provided confidence from a parse result, if any.

    MinerU currently returns no confidence field; when a deployment adds one
    (any whitelisted key anywhere in the raw payload), it is honored
    automatically for orientation voting.
    """

    raw = getattr(parsed, "raw", None)
    content_list = getattr(parsed, "content_list", None)
    values: list[float] = []

    def _walk(node: object) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if (
                    isinstance(key, str)
                    and key.casefold() in _CONFIDENCE_KEYS
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ):
                    values.append(float(value))
                else:
                    _walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)

    _walk(raw)
    if not values and content_list:
        _walk(content_list)
    if not values:
        return None
    return mean(values)


def _node_name_prompt(rows: tuple[NodeStringTableRow, ...]) -> str:
    """Build the Qwen vision prompt with the reviewed table candidates.

    Only Node names reach the model. The schedule table's string counts are
    irrelevant to reading a name and a count sitting next to a plausible
    name is exactly the kind of unrelated token a vision model copies back,
    so they are deliberately withheld.
    """

    candidates = [row.node for row in rows]
    return (
        "Read the Node/equipment name visible in this crop. The image is in "
        "its original drawing orientation; do not reinterpret it as rotated. "
        "The following Node names were extracted from the same page table:\n"
        + json.dumps(candidates, ensure_ascii=False)
        + "\nPrefer and return an exact Node name from this list whenever the "
        "visible label can reasonably correspond to it. If the crop shows no "
        "readable equipment name, or nothing that corresponds to a listed "
        f"name, return the literal {_NO_MATCH_MARKER} instead of guessing: "
        "quantities, dimensions, notes and other drawing annotations are "
        f"never Node names. Return only the name, or only {_NO_MATCH_MARKER}."
    )


def _is_no_match_completion(text: str) -> bool:
    """True when a completion is the reader's explicit no-match answer."""

    stripped = re.sub(r"[\s_\-]+", " ", str(text)).strip()
    stripped = stripped.strip("\"'`").rstrip("。.!！").strip()
    return not stripped or stripped.casefold() in _NO_MATCH_MARKERS


def _clean_node_name_completion(content: str) -> str:
    text = str(content).strip()
    fenced = re.fullmatch(r"```(?:text)?\s*(.*?)\s*```", text, re.S | re.I)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        return ""
    cleaned = text.strip().strip("\"'`").splitlines()[0].strip()
    # The reader's no-match answer becomes an empty reading: the pipeline
    # keeps its numbered placeholder and reports the Node for manual review
    # instead of treating "No Match" as an equipment name.
    return "" if _is_no_match_completion(cleaned) else cleaned


def _collect_node_crop_text(
    futures: list[Future],
) -> tuple[str, int, float | None, str | None, list[dict[str, object]]]:
    """Pick the best orientation reading from one Node's OCR futures.

    Ranking prefers a service-reported confidence, then the deterministic
    text-quality score, then the upright reading. The returned error is only
    set when every orientation request failed.
    """

    readings: list[tuple[str, int, float | None]] = []
    errors: list[str] = []
    debug_readings: list[dict[str, object]] = []
    for angle, future in futures:
        try:
            text, confidence = future.result()
        except Exception as exc:  # noqa: BLE001 - per-orientation failures
            errors.append(str(exc))
            debug_readings.append(
                {
                    "angle_deg": angle,
                    "text": "",
                    "confidence": None,
                    "error": str(exc),
                }
            )
            continue
        stripped = text.strip()
        readings.append((stripped, angle, confidence))
        debug_readings.append(
            {
                "angle_deg": angle,
                "text": stripped,
                "confidence": confidence,
                "error": None,
            }
        )
    if not readings:
        message = "; ".join(dict.fromkeys(errors))
        return (
            "",
            0,
            None,
            message or "OCR produced no reading.",
            debug_readings,
        )
    best = max(readings, key=_reading_sort_key)
    return (best[0], best[1], best[2], None, debug_readings)


def _reading_sort_key(entry: tuple[str, int, float | None]) -> tuple:
    text, angle, confidence = entry
    confidence_key = (
        (1, confidence) if confidence is not None else (0, 0.0)
    )
    return (confidence_key, _ocr_text_quality(text), -angle)


def _ocr_text_quality(text: str) -> tuple[int, int, int, int]:
    """Deterministic proxy score for readings without service confidence.

    Rewards readings shaped like one complete equipment name: a resolvable
    signature first, then absence of stray extra numbers (rotated/garbled
    readings tend to merge neighbouring labels), then INV/equipment tokens,
    then raw alphanumeric volume.
    """

    upper = re.sub(r"\s+", " ", text.upper())
    id_matches = list(_INV_ID_PATTERN.finditer(upper))
    standalone_numbers = [
        match.group(0)
        for match in _NUMBER_PATTERN.finditer(upper)
        if not any(
            start <= match.start() < end
            for start, end in (item.span() for item in id_matches)
        )
    ]
    # One sequence number per label; a pure-numeric INV legitimately adds a
    # second standalone number. Anything beyond that is merged-in noise.
    if id_matches:
        extra_noise = max(0, len(standalone_numbers) - 1)
    else:
        extra_noise = max(0, len(standalone_numbers) - 2)
    has_inv_token = bool(re.search(r"\bINV", upper)) or bool(id_matches)
    has_equipment_token = bool(_EQUIPMENT_TOKEN_PATTERN.search(upper))
    signature_valid = _equipment_signature(upper) is not None
    info = sum(1 for ch in upper if ch.isalnum())
    return (
        1 if signature_valid else 0,
        -extra_noise,
        (1 if has_inv_token else 0) + (1 if has_equipment_token else 0),
        min(info, 200),
    )


def _equipment_signature(text: str) -> tuple[str, str, int] | None:
    """Order-insensitive identity of one equipment name.

    ``"-CBX- INV11A101 01"`` and ``"INV11A101-CBX-01"`` both resolve to
    ``("11A101", "CBX", 1)`` regardless of token order or separators.
    """

    upper = re.sub(r"\s+", " ", str(text).upper())
    id_matches = list(_INV_ID_PATTERN.finditer(upper))
    inv_id = upper[id_matches[0].start() : id_matches[0].end()] if id_matches else ""
    standalone_numbers = [
        match.group(0)
        for match in _NUMBER_PATTERN.finditer(upper)
        if not any(
            start <= match.start() < end
            for start, end in (item.span() for item in id_matches)
        )
    ]
    if not standalone_numbers:
        return None
    sequence = int(standalone_numbers[-1])
    # Pure-numeric INVs carry no letter; fall back to the first standalone
    # number as the identifier whenever at least two numbers exist.
    fallback_id = standalone_numbers[0] if len(standalone_numbers) >= 2 else ""
    token_match = _EQUIPMENT_TOKEN_PATTERN.search(upper)
    equipment_token = token_match.group(1) if token_match else ""
    return (inv_id or fallback_id, equipment_token, sequence)


def _signatures_equal(
    left: tuple[str, str, int] | None,
    right: tuple[str, str, int] | None,
) -> bool:
    return _signature_matches(left, right)


def _signature_matches(
    left: tuple[str, str, int] | None,
    right: tuple[str, str, int] | None,
    *,
    fuzzy_id: bool = False,
) -> bool:
    if left is None or right is None:
        return False
    if left[0] != right[0]:
        # OCR sometimes inserts or drops one character inside the INV id
        # ("INV11A101" -> "INV111A101"); tolerate a single edit only when the
        # caller resolves it against an unambiguous table.
        if not (
            fuzzy_id
            and left[0]
            and right[0]
            and _one_edit_apart(left[0], right[0])
        ):
            return False
    if left[2] != right[2]:
        return False
    # A missing equipment token on either side stays tolerant; conflicting
    # tokens (CBX vs LBD) never match.
    return not left[1] or not right[1] or left[1] == right[1]


def _one_edit_apart(left: str, right: str) -> bool:
    """True when two identifiers differ by at most one inserted/deleted/changed character."""

    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    i = j = 0
    edited = False
    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            i += 1
            j += 1
            continue
        if edited:
            return False
        edited = True
        if len(left) == len(right):
            i += 1
        j += 1
    return True


def _normalize_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _extract_equipment_name(text: str) -> str:
    candidates = re.findall(r"[A-Z0-9][A-Z0-9 _-]*(?:CBX|LBD|LYNX)[A-Z0-9 _-]*", text.upper())
    if not candidates:
        candidates = re.findall(r"(?:CBX|LBD|LYNX)[-_ ]?[A-Z0-9_-]+", text.upper())
    if not candidates:
        return ""
    return re.sub(r"\s+", " ", candidates[0]).strip(" _-")


def _extract_inv_prefix(node_name: str) -> str:
    """Return the part of a Node name before its CBX/LBD token."""

    text = re.sub(r"\s+", " ", node_name.upper()).strip()
    # Only a letter continues an equipment token.  The separator itself is a
    # drawing detail ("-CBX-", "CBX.22", "CBX_01") and must never hide the
    # token, or every Node of a page that prints it stays unnamed.
    token = re.search(r"(?:CBX|LBD)(?![A-Z])", text)
    if token is None:
        return ""
    prefix = text[: token.start()].strip(" _-")
    if not prefix:
        # Some drawings print the equipment token first ("-CBX- INV11A101
        # 01"); the INV identifier then follows the token.
        return _inv_prefix_after_token(text[token.end() :])
    # OCR may retain a drawing/table prefix before the actual INV token. When
    # INV is present, it is the stable business-name boundary used by Agent2.
    inv_tokens = list(re.finditer(r"\bINV", prefix))
    if inv_tokens:
        prefix = prefix[inv_tokens[-1].start() :]
    prefix = re.sub(r"[\s_-]+", " ", prefix).strip()
    if prefix.startswith("INV "):
        return "INV " + re.sub(r"[^A-Z0-9]", "", prefix[4:])
    if prefix.startswith("INV"):
        return "INV " + re.sub(r"[^A-Z0-9]", "", prefix[3:])
    return prefix


def _inv_prefix_after_token(following: str) -> str:
    """Extract ``INV <id>`` from the text following a CBX/LBD token."""

    labeled = re.search(r"INV[^A-Z0-9]*([0-9]{1,6}[A-Z]?[0-9]{0,6})", following)
    if labeled is not None:
        return "INV " + re.sub(r"[^A-Z0-9]", "", labeled.group(1))
    # No INV keyword: only trust an alphanumeric block id (e.g. "11A101"),
    # never a bare trailing sequence number.
    bare = re.match(r"[^A-Z0-9]*([0-9]{1,6}[A-Z][0-9]{1,6})", following)
    if bare is not None:
        return "INV " + bare.group(1)
    return ""


def _match_table_name(text: str, table_by_name: dict[str, object], used: set[str]) -> str:
    normalized_text = _normalize_name(text)
    matches = [key for key in table_by_name if key not in used and key and key in normalized_text]
    if len(matches) == 1:
        row = table_by_name[matches[0]]
        return str(getattr(row, "node"))
    # Order-tolerant fallback: CAD labels and schedule rows may print the
    # same name with opposite token order (CBX-first vs INV-first), which a
    # directional substring test can never see.
    text_signature = _equipment_signature(text)
    if text_signature is not None:
        signature_matches = [
            key
            for key, row in table_by_name.items()
            if key not in used
            and _signature_matches(
                text_signature,
                _equipment_signature(str(getattr(row, "node"))),
                fuzzy_id=True,
            )
        ]
        if len(signature_matches) == 1:
            return str(getattr(table_by_name[signature_matches[0]], "node"))
    extracted = _normalize_name(_extract_equipment_name(text))
    if extracted in table_by_name and extracted not in used:
        return str(getattr(table_by_name[extracted], "node"))
    return ""


def _node_ocr_raw(
    raw_text: str,
    angle: int,
    confidence: float | None,
) -> dict[str, object]:
    """Annotation payload recording which orientation reading won."""

    payload: dict[str, object] = {
        "ocr_text": raw_text,
        "ocr_angle_deg": angle,
    }
    if confidence is not None:
        payload["ocr_confidence"] = round(confidence, 4)
    return payload
