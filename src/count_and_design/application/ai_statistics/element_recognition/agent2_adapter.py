"""Adapter from Agent2's overall-site output to Agent3 metadata."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Any

from .contracts import (
    InvExpectedStringCount,
    InvRecognitionSettings,
    InvStringCountColumn,
    PanelOrientation,
    TrackerDefinition,
)


@dataclass(frozen=True)
class Agent2ElementContract:
    """Typed Agent2 subset consumed by the Agent3 workflow boundary."""

    inv_names: tuple[str, ...]
    tracker_definitions: tuple[TrackerDefinition, ...]
    default_panel_orientation: PanelOrientation = PanelOrientation.UNKNOWN
    default_offset_snap_step_strings: float = 1.0
    inv_settings: tuple[InvRecognitionSettings, ...] = ()
    inv_string_counts: tuple[InvExpectedStringCount, ...] = ()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "Agent2ElementContract":
        """Adapt a synthetic/current Agent2 JSON-like payload."""

        inv_names = tuple(
            str(item).strip()
            for item in _sequence(payload.get("inv_names", ()), "inv_names")
            if str(item).strip()
        )
        trackers = tuple(
            TrackerDefinition(
                code=str(_mapping(item, "tracker_definitions[]")["code"]).strip(),
                strings_per_tracker=int(
                    _mapping(item, "tracker_definitions[]")["strings_per_tracker"]
                ),
                total_count=int(
                    _mapping(item, "tracker_definitions[]").get("total_count", 0)
                ),
                panel_label=str(
                    _mapping(item, "tracker_definitions[]").get("panel_label", "")
                ).strip(),
            )
            for item in _sequence(
                payload.get("tracker_definitions", ()),
                "tracker_definitions",
            )
        )
        default_orientation = _orientation(
            payload.get("default_panel_orientation", "unknown")
        )
        default_step = float(payload.get("default_offset_snap_step_strings", 1.0))
        inv_settings = tuple(
            InvRecognitionSettings(
                inv_name=str(_mapping(item, "inv_settings[]")["inv_name"]).strip(),
                panel_orientation=_optional_orientation(
                    _mapping(item, "inv_settings[]").get("panel_orientation")
                ),
                offset_snap_step_strings=float(
                    _mapping(item, "inv_settings[]").get(
                        "offset_snap_step_strings",
                        default_step,
                    )
                ),
            )
            for item in _sequence(payload.get("inv_settings", ()), "inv_settings")
        )
        if not trackers:
            raise ValueError("Agent2 must provide tracker_definitions.")
        if default_step <= 0 or any(
            item.offset_snap_step_strings <= 0 for item in inv_settings
        ):
            raise ValueError("Offset snap steps must be positive.")
        return cls(
            inv_names=inv_names,
            tracker_definitions=trackers,
            default_panel_orientation=default_orientation,
            default_offset_snap_step_strings=default_step,
            inv_settings=inv_settings,
            inv_string_counts=inv_string_counts_from_mapping(payload),
        )

    @classmethod
    def from_overall_site(
        cls,
        extractions: Sequence[Any],
        *,
        default_panel_orientation: PanelOrientation = PanelOrientation.UNKNOWN,
        default_offset_snap_step_strings: float = 1.0,
        inv_settings: Sequence[InvRecognitionSettings] = (),
    ) -> "Agent2ElementContract":
        """Adapt the reviewed Agent2 overall-site result shipped by v1.0-SIT.

        The authoritative INV list is passed to Agent3 for post-recognition
        validation. Agent3 derives page ownership from recognized Node names;
        this adapter never pairs pages and INVs by their order.
        """

        from ..info_recognition import build_agent4_input

        rows = tuple(extractions)
        known_invs = inv_names_from_overall_site(rows)
        payload = build_agent4_input(rows)
        module_columns = {
            str(item.get("column_id", "")): item
            for item in payload.get("module_columns", ())
            if isinstance(item, Mapping)
        }
        trackers: list[TrackerDefinition] = []
        for item in payload.get("tracker_totals", ()):
            if not isinstance(item, Mapping):
                continue
            code = str(item.get("tracker_type", "")).strip()
            strings_per_tracker = _tracker_string_count(code)
            if not code or strings_per_tracker is None:
                continue
            module_counts = item.get("module_counts", {})
            panel_label = ""
            if isinstance(module_counts, Mapping):
                for column_id, count in module_counts.items():
                    if not isinstance(count, int):
                        continue
                    column = module_columns.get(str(column_id), {})
                    panel_label = str(
                        column.get("module_model")
                        or column.get("module_stc_dc_rating_w")
                        or ""
                    ).strip()
                    if panel_label:
                        break
            total = item.get("total", 0)
            trackers.append(
                TrackerDefinition(
                    code=code,
                    strings_per_tracker=strings_per_tracker,
                    total_count=total if isinstance(total, int) else 0,
                    panel_label=panel_label,
                )
            )
        if not trackers:
            raise ValueError(
                "Agent2 overall-site result contains no usable tracker definitions."
            )
        if default_offset_snap_step_strings <= 0:
            raise ValueError("Offset snap steps must be positive.")
        return cls(
            inv_names=known_invs,
            tracker_definitions=tuple(trackers),
            default_panel_orientation=default_panel_orientation,
            default_offset_snap_step_strings=default_offset_snap_step_strings,
            inv_settings=tuple(inv_settings),
            inv_string_counts=inv_string_counts_from_overall_site(rows),
        )


def inv_names_from_overall_site(extractions: Sequence[Any]) -> tuple[str, ...]:
    """Return Agent2 INV names in reviewed first-occurrence order."""

    names: list[str] = []
    for extraction in extractions:
        raw_values = getattr(extraction, "inv_list", ())
        if (
            isinstance(raw_values, (str, bytes))
            or not isinstance(raw_values, Sequence)
        ):
            continue
        for value in raw_values:
            name = str(value).strip()
            if name and name not in names:
                names.append(name)
    return tuple(names)


def inv_string_counts_from_overall_site(
    extractions: Sequence[Any],
) -> tuple[InvExpectedStringCount, ...]:
    """Return Agent2 per-INV string counts in reviewed first-occurrence order."""

    rows_by_name: dict[str, Any] = {}
    for extraction in extractions:
        raw_rows = getattr(extraction, "inv_rows", ())
        if (
            isinstance(raw_rows, (str, bytes))
            or not isinstance(raw_rows, Sequence)
        ):
            continue
        for row in raw_rows:
            name = str(getattr(row, "identifier", "")).strip()
            if name and name not in rows_by_name:
                rows_by_name[name] = row
    counts: list[InvExpectedStringCount] = []
    for name in inv_names_from_overall_site(extractions):
        row = rows_by_name.get(name)
        columns: list[InvStringCountColumn] = []
        if row is not None:
            for item in getattr(row, "string_counts", ()):
                label = str(getattr(item, "label", "")).strip()
                if not label:
                    continue
                columns.append(
                    InvStringCountColumn(
                        label=label,
                        count=_optional_int(getattr(item, "count", None)),
                        column_index=int(getattr(item, "column_index", 0) or 0),
                    )
                )
        counts.append(InvExpectedStringCount(inv_name=name, columns=tuple(columns)))
    return tuple(counts)


def inv_string_counts_from_mapping(
    payload: Mapping[str, Any],
) -> tuple[InvExpectedStringCount, ...]:
    if "inv_string_counts" not in payload:
        return ()
    return tuple(
        InvExpectedStringCount(
            inv_name=str(_mapping(item, "inv_string_counts[]")["inv_name"]).strip(),
            columns=tuple(
                InvStringCountColumn(
                    label=str(_mapping(column, "columns[]").get("label", "")).strip(),
                    count=_optional_int(_mapping(column, "columns[]").get("count")),
                    column_index=int(
                        _mapping(column, "columns[]").get("column_index", 0) or 0
                    ),
                )
                for column in _sequence(
                    _mapping(item, "inv_string_counts[]").get("columns", ()),
                    "columns",
                )
            ),
        )
        for item in _sequence(payload.get("inv_string_counts", ()), "inv_string_counts")
        if str(_mapping(item, "inv_string_counts[]").get("inv_name", "")).strip()
    )


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().replace(",", "")
    return int(text) if text.isdigit() else None


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object.")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be a list.")
    return value


def _orientation(value: Any) -> PanelOrientation:
    try:
        return PanelOrientation.from_value(value)
    except ValueError as exc:
        raise ValueError(f"Unknown panel orientation: {value}") from exc


def _optional_orientation(value: Any) -> PanelOrientation | None:
    if value is None or str(value).strip() == "":
        return None
    return _orientation(value)


def _tracker_string_count(code: str) -> int | None:
    match = re.search(r"\d+", code)
    return int(match.group(0)) if match else None
