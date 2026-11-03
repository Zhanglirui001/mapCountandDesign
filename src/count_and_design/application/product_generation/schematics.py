"""SVG schematic export for concrete racking/product occurrences."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING
import xml.etree.ElementTree as ET

from ...i18n import tr
from ...models.assembled_product import AssembledProduct
from ...models.part import Part
from .result import GeneratedPresetProducts, GeneratedRackingInstance

if TYPE_CHECKING:
    from ...models.product_preset import ProductPreset
    from ...models.project import Project
    from .result import ProductGenerationResult


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

MODULE_COLLAPSE_THRESHOLD = 30
MODULE_VISIBLE_AT_EACH_END = 4

_PANEL_WIDTH = 48.0
_PANEL_HEIGHT = 24.0
_PANEL_GAP = 2.5
_STRING_GAP = 120.0
_EXTERNAL_ORIGIN_X = 70.0
_MIN_CANVAS_WIDTH = 1160.0
_MIN_CANVAS_HEIGHT = 480.0
_TOP_MARGIN = 100.0
_BOTTOM_MARGIN = 70.0
_COMPONENT_PITCH = 34.0
_SECTION_HEIGHT = 350.0
_POSITIVE_LANE_OFFSET = 95.0
_PANEL_ROW_OFFSET = 185.0
_NEGATIVE_LANE_OFFSET = 275.0


@dataclass(frozen=True)
class SchematicString:
    preset_name: str
    branch_index: int
    panel_name: str
    module_count: int
    positive_terminal: str
    negative_terminal: str
    warning: str = ""


@dataclass(frozen=True)
class SchematicSection:
    preset_name: str
    product_type: str
    positive: AssembledProduct | None
    negative: AssembledProduct | None
    strings: tuple[SchematicString, ...]
    crossed_presets: str = ""
    crossed_length_ft: float = 0.0
    warning: str = ""


@dataclass
class SchematicVariant:
    rackings_name: str
    sections: tuple[SchematicSection, ...]
    warnings: tuple[str, ...]
    sources: list[str] = field(default_factory=list)
    variant_number: int = 1


@dataclass(frozen=True)
class SchematicExportResult:
    directory: str
    paths: tuple[str, ...]
    variant_count: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _HarnessTopology:
    prefix: tuple[Part, ...]
    tee_branches: tuple[tuple[Part, ...], ...]
    between: tuple[tuple[Part, ...], ...]
    last_branch: tuple[Part, ...]
    is_homerun: bool = False


def _part_is_visible(part: Part) -> bool:
    return part.category != "Label"


def _flatten_visible(part: Part) -> tuple[Part, ...]:
    if part.category == "Label":
        return ()
    if part.semi_or_not:
        flattened: list[Part] = []
        for child in part.semi_list:
            flattened.extend(_flatten_visible(child))
        return tuple(flattened)
    return (part,)


def _is_tee_semi(part: Part) -> bool:
    return bool(
        part.semi_or_not
        and part.semi_list
        and part.semi_list[0].category == "T Kit"
    )


def _tee_branch_parts(part: Part) -> tuple[Part, ...]:
    parts: list[Part] = []
    for child in part.semi_list[1:]:
        parts.extend(_flatten_visible(child))
    return tuple(parts)


def _build_topology(product: AssembledProduct | None) -> _HarnessTopology:
    if product is None:
        return _HarnessTopology((), (), (), ())

    tee_positions = [
        index for index, part in enumerate(product.parts) if _is_tee_semi(part)
    ]
    if not tee_positions:
        branch: list[Part] = []
        for part in product.parts:
            branch.extend(_flatten_visible(part))
        return _HarnessTopology((), (), (), tuple(branch), is_homerun=True)

    prefix: list[Part] = []
    for part in product.parts[: tee_positions[0]]:
        prefix.extend(_flatten_visible(part))

    tee_branches = tuple(
        _tee_branch_parts(product.parts[index]) for index in tee_positions
    )
    between: list[tuple[Part, ...]] = []
    for current, following in zip(tee_positions, tee_positions[1:]):
        segment: list[Part] = []
        for part in product.parts[current + 1 : following]:
            segment.extend(_flatten_visible(part))
        between.append(tuple(segment))

    trailing: list[Part] = []
    for part in product.parts[tee_positions[-1] + 1 :]:
        trailing.extend(_flatten_visible(part))

    return _HarnessTopology(
        prefix=tuple(prefix),
        tee_branches=tee_branches,
        between=tuple(between),
        last_branch=tuple(trailing),
    )


def _part_signature(part: Part) -> tuple[object, ...] | None:
    if part.category == "Label":
        return None
    return (
        part.category,
        part.cad_parts_no,
        part.description,
        round(part.length, 4),
        tuple(
            signature
            for child in part.semi_list
            if (signature := _part_signature(child)) is not None
        ),
    )


def _product_signature(product: AssembledProduct | None) -> tuple[object, ...]:
    if product is None:
        return ()
    return (
        product.polarity,
        product.code,
        tuple(
            signature
            for part in product.parts
            if (signature := _part_signature(part)) is not None
        ),
    )


def _variant_signature(
    sections: Iterable[SchematicSection],
) -> tuple[object, ...]:
    return tuple(
        (
            section.preset_name,
            section.product_type,
            tuple(
                (
                    string.panel_name,
                    string.module_count,
                    string.positive_terminal,
                    string.negative_terminal,
                    bool(string.warning),
                )
                for string in section.strings
            ),
            _product_signature(section.positive),
            _product_signature(section.negative),
            section.crossed_presets,
            round(section.crossed_length_ft, 4),
            bool(section.warning),
        )
        for section in sections
    )


def _terminal_sides(preset: ProductPreset, branch_index: int) -> tuple[str, str]:
    """Return POS and NEG terminal sides (``head``/``tail``)."""
    if branch_index >= len(preset.branch_lines):
        return "head", "head"
    coordinate = preset.branch_lines[branch_index]
    positive = coordinate.coordinate_positive.to_mm()
    negative = coordinate.coordinate_negative.to_mm()
    if positive > negative:
        return "tail", "head"
    if negative > positive:
        return "head", "tail"
    return "head", "head"


def _source_label(instance: GeneratedRackingInstance) -> str:
    return (
        f"{instance.inv_name} / {instance.node_name} / {instance.row_name} / "
        f"Racking {instance.entry_index + 1}"
    )


def _pair_by_index(
    instance: GeneratedRackingInstance,
) -> dict[int, GeneratedPresetProducts]:
    return {pair.preset_index: pair for pair in instance.preset_products}


def _build_instance_variant(
    instance: GeneratedRackingInstance,
    project: Project,
) -> SchematicVariant:
    sections: list[SchematicSection] = []
    warnings: list[str] = []
    pairs = _pair_by_index(instance)
    source = _source_label(instance)

    for preset_index, preset_name in enumerate(instance.preset_names):
        preset = project.get_product_preset(preset_name)
        if preset is None:
            warnings.append(
                tr(
                    "schematics.warning.missing_preset",
                    "Preset '{name}' referenced by the racking was not found.",
                ).format(name=preset_name)
            )
            continue
        if preset.product_type not in ("IBEX", "Homerun_Extender"):
            continue

        pair = pairs.get(preset_index)
        product_type = pair.product_type if pair is not None else preset.product_type
        positive = pair.positive if pair is not None else None
        negative = pair.negative if pair is not None else None
        section_warning = ""

        if preset.branch_count <= 0:
            section_warning = tr(
                "schematics.warning.no_branches",
                "Preset '{name}' has no branches.",
            ).format(name=preset.name)
            warnings.append(section_warning)

        panel = project.get_pv_panel(preset.pv_panel) if preset.pv_panel.strip() else None
        panel_warning = ""
        if panel is None or panel.module_per_string <= 0:
            panel_warning = tr(
                "schematics.warning.invalid_panel",
                "Preset '{name}' has no valid PV panel/string module count.",
            ).format(name=preset.name)
            warnings.append(panel_warning)

        strings: list[SchematicString] = []
        for branch_index in range(max(preset.branch_count, 0)):
            positive_terminal, negative_terminal = _terminal_sides(
                preset, branch_index
            )
            strings.append(
                SchematicString(
                    preset_name=preset.name,
                    branch_index=branch_index,
                    panel_name=panel.name if panel is not None else preset.pv_panel,
                    module_count=(
                        panel.module_per_string
                        if panel is not None and panel.module_per_string > 0
                        else 0
                    ),
                    positive_terminal=positive_terminal,
                    negative_terminal=negative_terminal,
                    warning=panel_warning,
                )
            )

        sections.append(
            SchematicSection(
                preset_name=preset.name,
                product_type=product_type,
                positive=positive,
                negative=negative,
                strings=tuple(strings),
                crossed_presets=(
                    positive.crossed_presets
                    if positive is not None
                    else negative.crossed_presets
                    if negative is not None
                    else ""
                ),
                crossed_length_ft=(
                    positive.extender_ft
                    if positive is not None
                    else negative.extender_ft
                    if negative is not None
                    else 0.0
                ),
                warning=section_warning,
            )
        )

    if not any(section.positive or section.negative for section in sections):
        warning = tr(
            "schematics.warning.no_products",
            "No drawable IBEX or Homerun products were generated.",
        )
        warnings.append(warning)

    return SchematicVariant(
        rackings_name=instance.rackings_name,
        sections=tuple(sections),
        warnings=tuple(dict.fromkeys(warnings)),
        sources=[source],
    )


def build_schematic_variants(
    result: ProductGenerationResult,
    project: Project,
) -> list[SchematicVariant]:
    """Build and deduplicate visible schematic variants."""
    variants: list[SchematicVariant] = []
    by_key: dict[tuple[str, tuple[object, ...]], SchematicVariant] = {}
    counts_by_racking: dict[str, int] = {}

    for instance in result.racking_instances:
        candidate = _build_instance_variant(instance, project)
        signature = _variant_signature(candidate.sections)
        key = (candidate.rackings_name, signature)
        existing = by_key.get(key)
        if existing is not None:
            existing.sources.extend(candidate.sources)
            continue
        number = counts_by_racking.get(candidate.rackings_name, 0) + 1
        counts_by_racking[candidate.rackings_name] = number
        candidate.variant_number = number
        by_key[key] = candidate
        variants.append(candidate)

    return variants


def _svg_element(
    parent: ET.Element,
    tag: str,
    attributes: dict[str, object] | None = None,
    text: str | None = None,
) -> ET.Element:
    element = ET.SubElement(
        parent,
        f"{{{SVG_NS}}}{tag}",
        {key: str(value) for key, value in (attributes or {}).items()},
    )
    if text is not None:
        element.text = text
    return element


def _add_text(
    parent: ET.Element,
    x: float,
    y: float,
    text: str,
    css_class: str = "",
    **attributes: object,
) -> ET.Element:
    attrs: dict[str, object] = {"x": x, "y": y}
    if css_class:
        attrs["class"] = css_class
    attrs.update(attributes)
    return _svg_element(parent, "text", attrs, text)


def _displayed_module_rows(module_count: int) -> tuple[str, ...]:
    if module_count <= 0:
        return ()
    if module_count <= MODULE_COLLAPSE_THRESHOLD:
        return tuple("panel" for _ in range(module_count))
    return (
        *("panel" for _ in range(MODULE_VISIBLE_AT_EACH_END)),
        "ellipsis",
        *("panel" for _ in range(MODULE_VISIBLE_AT_EACH_END)),
    )


def _string_width(string: SchematicString) -> float:
    if string.module_count <= 0:
        return 72.0
    columns = _displayed_module_rows(string.module_count)
    width = 0.0
    for column in columns:
        width += 31.0 if column == "ellipsis" else _PANEL_WIDTH + _PANEL_GAP
    return max(width - _PANEL_GAP, 72.0)


def _draw_string(
    parent: ET.Element,
    string: SchematicString,
    left: float,
    y: float,
) -> tuple[float, float]:
    width = _string_width(string)
    group = _svg_element(
        parent,
        "g",
        {
            "class": "pv-string",
            "data-preset": string.preset_name,
            "data-branch-index": string.branch_index + 1,
            "data-module-count": string.module_count,
        },
    )
    _add_text(
        group,
        left,
        y - 25,
        f"{string.preset_name}-{string.branch_index + 1}",
        "string-label",
    )

    if string.module_count <= 0:
        _svg_element(
            group,
            "rect",
            {
                "class": "warning panel-placeholder",
                "x": left,
                "y": y,
                "width": width,
                "height": _PANEL_HEIGHT,
                "rx": 4,
            },
        )
        _add_text(
            group,
            left + width / 2,
            y + 17,
            "?",
            "warning-symbol",
            **{"text-anchor": "middle"},
        )
        _add_text(group, left, y + 39, string.warning, "warning-text")
    else:
        x = left
        panel_number = 1
        columns = _displayed_module_rows(string.module_count)
        for column in columns:
            if column == "ellipsis":
                _add_text(
                    group,
                    x + 15.5,
                    y + 17,
                    "…",
                    "panel-ellipsis",
                    **{"text-anchor": "middle"},
                )
                x += 31.0
                panel_number = string.module_count - MODULE_VISIBLE_AT_EACH_END + 1
                continue
            _svg_element(
                group,
                "rect",
                {
                    "class": "panel",
                    "x": x,
                    "y": y,
                    "width": _PANEL_WIDTH,
                    "height": _PANEL_HEIGHT,
                    "rx": 1.5,
                    "data-panel-index": panel_number,
                },
            )
            panel_number += 1
            x += _PANEL_WIDTH + _PANEL_GAP
        if string.module_count > MODULE_COLLAPSE_THRESHOLD:
            _add_text(
                group,
                left + width / 2,
                y + _PANEL_HEIGHT + 18,
                tr(
                    "schematics.modules_total",
                    "{count} modules total",
                ).format(count=string.module_count),
                "module-count",
                **{"text-anchor": "middle"},
            )

    panel_label = string.panel_name.strip() or tr(
        "schematics.panel_unknown", "PV panel not set"
    )
    _add_text(
        group,
        left,
        y - 9,
        panel_label,
        "panel-name",
    )
    return left, left + width


def _format_ft(value: float) -> str:
    return f"{round(value, 4):.4f}".rstrip("0").rstrip(".") or "0"


def _wire_label(part: Part) -> str:
    return f"{part.description} · {_format_ft(part.length)} ft"


def _draw_wire(
    parent: ET.Element,
    part: Part,
    polarity: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    label_offset: float = -7.0,
) -> None:
    css = f"wire {polarity.lower()}"
    group = _svg_element(
        parent,
        "g",
        {
            "class": css,
            "data-description": part.description,
            "data-length-ft": _format_ft(part.length),
        },
    )
    dx = x2 - x1
    dy = y2 - y1
    length = max((dx * dx + dy * dy) ** 0.5, 1.0)
    offset_x = -dy / length * 2.2
    offset_y = dx / length * 2.2
    for multiplier in (-1, 1):
        _svg_element(
            group,
            "line",
            {
                "x1": x1 + offset_x * multiplier,
                "y1": y1 + offset_y * multiplier,
                "x2": x2 + offset_x * multiplier,
                "y2": y2 + offset_y * multiplier,
            },
        )
    _svg_element(group, "title", text=_wire_label(part))
    midpoint_x = (x1 + x2) / 2
    midpoint_y = (y1 + y2) / 2
    attributes: dict[str, object] = {"text-anchor": "middle"}
    if abs(dy) > abs(dx):
        attributes["transform"] = (
            f"rotate(-90 {midpoint_x + label_offset} {midpoint_y})"
        )
        midpoint_x += label_offset
    else:
        midpoint_y += label_offset
    _add_text(
        group,
        midpoint_x,
        midpoint_y,
        _wire_label(part),
        "wire-label",
        **attributes,
    )


def _draw_connector(
    parent: ET.Element,
    part: Part,
    polarity: str,
    x: float,
    y: float,
) -> None:
    male = "Male" in part.cad_parts_no
    gender = "male" if male else "female"
    group = _svg_element(
        parent,
        "g",
        {
            "class": f"connector {gender} {polarity.lower()}",
            "data-description": part.description,
        },
    )
    _svg_element(
        group,
        "rect",
        {"x": x - 13, "y": y - 10, "width": 26, "height": 20, "rx": 2},
    )
    if male:
        _svg_element(
            group,
            "rect",
            {"class": "connector-feature", "x": x + 13, "y": y - 4, "width": 8, "height": 8},
        )
    else:
        _svg_element(
            group,
            "path",
            {
                "class": "connector-feature",
                "d": f"M {x + 13} {y - 5} H {x + 5} V {y + 5} H {x + 13}",
            },
        )
    _svg_element(group, "title", text=part.description)
    _add_text(
        group,
        x,
        y - 15,
        "M" if male else "F",
        "component-label",
        **{"text-anchor": "middle"},
    )


def _draw_box_component(
    parent: ET.Element,
    part: Part,
    polarity: str,
    x: float,
    y: float,
) -> None:
    if part.category == "Fuse":
        css_class = "fuse"
        label = "Fuse"
        width = 36
    elif part.category == "AC":
        css_class = "cu-al"
        label = "Cu-Al"
        width = 44
    else:
        css_class = "component"
        label = part.category or part.cad_parts_no or "Part"
        width = 44
    group = _svg_element(
        parent,
        "g",
        {
            "class": f"{css_class} {polarity.lower()}",
            "data-description": part.description,
        },
    )
    _svg_element(
        group,
        "rect",
        {"x": x - width / 2, "y": y - 11, "width": width, "height": 22, "rx": 2},
    )
    _add_text(
        group,
        x,
        y + 3.5,
        label,
        "component-label",
        **{"text-anchor": "middle"},
    )
    _svg_element(group, "title", text=part.description)


def _draw_part_at(
    parent: ET.Element,
    part: Part,
    polarity: str,
    x: float,
    y: float,
) -> None:
    if part.category == "Connect":
        _draw_connector(parent, part, polarity, x, y)
    else:
        _draw_box_component(parent, part, polarity, x, y)


def _draw_parallel_guide(
    parent: ET.Element,
    polarity: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> None:
    """Draw topology continuity without inventing a solid cable segment."""
    group = _svg_element(
        parent,
        "g",
        {"class": f"connection-guide {polarity.lower()}"},
    )
    dx = x2 - x1
    dy = y2 - y1
    length = max((dx * dx + dy * dy) ** 0.5, 1.0)
    offset_x = -dy / length * 2.2
    offset_y = dx / length * 2.2
    for multiplier in (-1, 1):
        _svg_element(
            group,
            "line",
            {
                "x1": x1 + offset_x * multiplier,
                "y1": y1 + offset_y * multiplier,
                "x2": x2 + offset_x * multiplier,
                "y2": y2 + offset_y * multiplier,
            },
        )


def _draw_sequence_horizontal(
    parent: ET.Element,
    parts: tuple[Part, ...],
    polarity: str,
    x1: float,
    x2: float,
    y: float,
) -> None:
    visible = tuple(part for part in parts if _part_is_visible(part))
    if not visible:
        _draw_parallel_guide(parent, polarity, x1, y, x2, y)
        return
    step = (x2 - x1) / len(visible)
    for index, part in enumerate(visible):
        start = x1 + step * index
        end = x1 + step * (index + 1)
        if part.category == "Wire":
            _draw_wire(parent, part, polarity, start, y, end, y)
        else:
            _draw_parallel_guide(parent, polarity, start, y, end, y)
            _draw_part_at(parent, part, polarity, (start + end) / 2, y)


def _draw_sequence_vertical(
    parent: ET.Element,
    parts: tuple[Part, ...],
    polarity: str,
    x: float,
    y1: float,
    y2: float,
) -> None:
    visible = tuple(part for part in parts if _part_is_visible(part))
    if not visible:
        _draw_parallel_guide(parent, polarity, x, y1, x, y2)
        return
    step = (y2 - y1) / len(visible)
    for index, part in enumerate(visible):
        start = y1 + step * index
        end = y1 + step * (index + 1)
        if part.category == "Wire":
            _draw_wire(
                parent,
                part,
                polarity,
                x,
                start,
                x,
                end,
                label_offset=18.0 if polarity == "POS" else -18.0,
            )
        else:
            _draw_parallel_guide(parent, polarity, x, start, x, end)
            _draw_part_at(parent, part, polarity, x, (start + end) / 2)


def _draw_tee_horizontal(
    parent: ET.Element,
    polarity: str,
    x: float,
    y: float,
    panel_y: float,
) -> None:
    if panel_y > y:
        points = (
            f"{x - 13},{y - 8} {x + 13},{y - 8} "
            f"{x + 13},{y + 8} {x + 5},{y + 8} "
            f"{x + 5},{y + 18} {x - 5},{y + 18} "
            f"{x - 5},{y + 8} {x - 13},{y + 8}"
        )
    else:
        points = (
            f"{x - 13},{y - 8} {x - 5},{y - 8} "
            f"{x - 5},{y - 18} {x + 5},{y - 18} "
            f"{x + 5},{y - 8} {x + 13},{y - 8} "
            f"{x + 13},{y + 8} {x - 13},{y + 8}"
        )
    _svg_element(
        parent,
        "polygon",
        {
            "class": f"tee {polarity.lower()}",
            "points": points,
        },
    )


def _terminal_x(
    string_positions: list[tuple[float, float]],
    string: SchematicString,
    polarity: str,
) -> float:
    head, tail = string_positions[string.branch_index]
    side = (
        string.positive_terminal
        if polarity == "POS"
        else string.negative_terminal
    )
    result = tail if side == "tail" else head
    if string.positive_terminal == string.negative_terminal:
        # The logical terminal is shared. A tiny visual offset keeps the
        # red and black branch lines distinguishable.
        result += -3.0 if polarity == "POS" else 3.0
    return result


def _draw_terminal(
    parent: ET.Element,
    string: SchematicString,
    x: float,
    y: float,
    polarity: str,
) -> None:
    equal = string.positive_terminal == string.negative_terminal
    if equal:
        y += -3 if polarity == "POS" else 3
    _svg_element(
        parent,
        "circle",
        {
            "class": f"terminal {polarity.lower()}",
            "cx": x,
            "cy": y,
            "r": 4,
            "data-terminal": (
                string.positive_terminal
                if polarity == "POS"
                else string.negative_terminal
            ),
        },
    )


def _draw_harness(
    parent: ET.Element,
    section: SchematicSection,
    string_positions: list[tuple[float, float]],
    polarity: str,
    lane_y: float,
    panel_y: float,
    external_origin_x: float,
    header_x: float,
) -> None:
    product = section.positive if polarity == "POS" else section.negative
    topology = _build_topology(product)
    if not section.strings:
        return

    targets = [
        _terminal_x(string_positions, string, polarity)
        for string in section.strings
    ]
    group = _svg_element(
        parent,
        "g",
        {
            "class": f"harness {polarity.lower()}",
            "data-preset": section.preset_name,
            "data-product-code": product.code if product is not None else "",
            "data-crossed-presets": section.crossed_presets,
            "data-crossed-length-ft": _format_ft(section.crossed_length_ft),
            "data-external-origin-x": f"{external_origin_x:g}",
        },
    )
    _add_text(
        group,
        header_x,
        lane_y - 24.0,
        (
            f"{section.preset_name} · {polarity} · "
            f"{product.code if product is not None else '—'}"
        ),
        f"product-label {polarity.lower()}",
    )
    if section.crossed_length_ft > 0:
        crossed = section.crossed_presets or "—"
        _add_text(
            group,
            header_x,
            lane_y - 9.0,
            tr(
                "schematics.crossed_length",
                "Crosses {presets} · +{length} ft",
            ).format(
                presets=crossed,
                length=_format_ft(section.crossed_length_ft),
            ),
            "crossed-label",
        )

    if product is None:
        _add_text(
            group,
            header_x,
            lane_y + 18.0,
            tr("schematics.product_missing", "Product not generated"),
            "warning-text",
        )
        return

    if topology.is_homerun:
        target = targets[0]
        _draw_sequence_horizontal(
            group,
            topology.last_branch,
            polarity,
            external_origin_x,
            target,
            lane_y,
        )
        _draw_parallel_guide(
            group,
            polarity,
            target,
            lane_y,
            target,
            panel_y + _PANEL_HEIGHT / 2,
        )
        _draw_terminal(
            group,
            section.strings[0],
            target,
            panel_y + _PANEL_HEIGHT / 2,
            polarity,
        )
        return

    tee_count = min(len(topology.tee_branches), max(len(targets) - 1, 0))
    if tee_count == 0:
        target = targets[0]
        _draw_sequence_horizontal(
            group,
            topology.last_branch or topology.prefix,
            polarity,
            external_origin_x,
            target,
            lane_y,
        )
        _draw_parallel_guide(
            group,
            polarity,
            target,
            lane_y,
            target,
            panel_y + _PANEL_HEIGHT / 2,
        )
        _draw_terminal(
            group,
            section.strings[0],
            target,
            panel_y + _PANEL_HEIGHT / 2,
            polarity,
        )
        return

    first_target = targets[0]
    _draw_sequence_horizontal(
        group,
        topology.prefix,
        polarity,
        external_origin_x,
        first_target,
        lane_y,
    )

    for branch_index in range(tee_count):
        target = targets[branch_index]
        _draw_tee_horizontal(group, polarity, target, lane_y, panel_y)
        _draw_sequence_vertical(
            group,
            topology.tee_branches[branch_index],
            polarity,
            target,
            lane_y,
            panel_y + _PANEL_HEIGHT / 2,
        )
        _draw_terminal(
            group,
            section.strings[branch_index],
            target,
            panel_y + _PANEL_HEIGHT / 2,
            polarity,
        )
        if branch_index < tee_count - 1:
            next_target = targets[branch_index + 1]
            parts = (
                topology.between[branch_index]
                if branch_index < len(topology.between)
                else ()
            )
            _draw_sequence_horizontal(
                group,
                parts,
                polarity,
                target,
                next_target,
                lane_y,
            )

    final_index = min(tee_count, len(section.strings) - 1)
    final_target = targets[final_index]
    _draw_sequence_horizontal(
        group,
        topology.last_branch,
        polarity,
        targets[tee_count - 1],
        final_target,
        lane_y,
    )
    _draw_parallel_guide(
        group,
        polarity,
        final_target,
        lane_y,
        final_target,
        panel_y + _PANEL_HEIGHT / 2,
    )
    _draw_terminal(
        group,
        section.strings[final_index],
        final_target,
        panel_y + _PANEL_HEIGHT / 2,
        polarity,
    )


def _external_component_count(section: SchematicSection) -> int:
    counts: list[int] = []
    for product in (section.positive, section.negative):
        topology = _build_topology(product)
        counts.append(
            len(topology.last_branch)
            if topology.is_homerun
            else len(topology.prefix)
        )
    return max(counts, default=0)


def _render_svg(variant: SchematicVariant) -> str:
    external_clearance = max(
        210.0,
        max(
            (_external_component_count(section) for section in variant.sections),
            default=0,
        )
        * _COMPONENT_PITCH
        + 70.0,
    )
    panel_start_x = _EXTERNAL_ORIGIN_X + external_clearance
    section_layouts: list[
        tuple[
            SchematicSection,
            float,
            list[tuple[float, float]],
            float,
        ]
    ] = []
    current_y = _TOP_MARGIN
    widest_section = panel_start_x
    for section in variant.sections:
        section_top = current_y
        panel_y = section_top + _PANEL_ROW_OFFSET
        positions: list[tuple[float, float]] = []
        current_x = panel_start_x
        for string in section.strings:
            width = _string_width(string)
            positions.append((current_x, current_x + width))
            current_x += width + _STRING_GAP
        if positions:
            current_x -= _STRING_GAP
        widest_section = max(widest_section, current_x)
        section_bottom = section_top + _SECTION_HEIGHT
        section_layouts.append(
            (section, section_top, positions, panel_y)
        )
        current_y = section_bottom + 35.0

    warning_height = max(len(variant.warnings), 1) * 21.0 + 35.0
    canvas_width = max(_MIN_CANVAS_WIDTH, widest_section + 85.0)
    canvas_height = max(
        current_y + warning_height + _BOTTOM_MARGIN,
        _MIN_CANVAS_HEIGHT,
    )
    root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "viewBox": f"0 0 {canvas_width:g} {canvas_height:g}",
            "width": f"{canvas_width:g}",
            "height": f"{canvas_height:g}",
            "role": "img",
            "aria-labelledby": "schematic-title schematic-description",
        },
    )
    _svg_element(
        root,
        "title",
        {"id": "schematic-title"},
        variant.rackings_name,
    )
    _svg_element(
        root,
        "desc",
        {"id": "schematic-description"},
        tr(
            "schematics.description",
            "PV racking and product schematic with positive and negative harnesses.",
        ),
    )
    style = """
svg { background: #fff; color: #222; font-family: Arial, "Microsoft YaHei", sans-serif; }
text { fill: #222; font-size: 12px; }
.title { font-size: 24px; font-weight: 700; }
.subtitle { fill: #666; font-size: 12px; }
.section-label { font-size: 16px; font-weight: 700; }
.section-rule { stroke: #d6d6d6; stroke-width: 1; }
.panel { fill: #b5b5b5; stroke: #666; stroke-width: 1; }
.panel-name, .string-label, .module-count { fill: #555; font-size: 10px; }
.panel-ellipsis { font-size: 22px; }
.panel-placeholder { fill: #fff5dc; stroke: #d18b00; stroke-width: 1.5; stroke-dasharray: 5 3; }
.warning-symbol { fill: #b26a00; font-size: 24px; font-weight: 700; }
.warning-text { fill: #9a5b00; font-size: 9px; }
.wire line { fill: none; stroke-width: 1.6; }
.wire.pos line, .connection-guide.pos line { stroke: #d32f2f; }
.wire.neg line, .connection-guide.neg line { stroke: #202020; }
.connection-guide line { fill: none; stroke-width: 1.4; }
.wire-label { font-size: 8.5px; paint-order: stroke; stroke: white; stroke-width: 3px; stroke-linejoin: round; }
.product-label { font-size: 12px; font-weight: 700; }
.product-label.pos { fill: #d32f2f; }
.product-label.neg { fill: #202020; }
.crossed-label { fill: #666; font-size: 9px; }
.connector rect, .connector path, .fuse rect, .cu-al rect, .component rect, .tee { fill: white; stroke-width: 2; }
.connector.pos rect, .connector.pos path, .fuse.pos rect, .cu-al.pos rect, .component.pos rect, .tee.pos { stroke: #d32f2f; }
.connector.neg rect, .connector.neg path, .fuse.neg rect, .cu-al.neg rect, .component.neg rect, .tee.neg { stroke: #202020; }
.connector-feature { fill: white; }
.component-label { font-size: 8px; }
.terminal.pos { fill: #d32f2f; stroke: white; stroke-width: 1; }
.terminal.neg { fill: #202020; stroke: white; stroke-width: 1; }
.warnings-title { fill: #9a5b00; font-weight: 700; }
.warning-line { fill: #9a5b00; font-size: 11px; }
"""
    _svg_element(root, "style", text=style)

    _add_text(root, 34, 42, variant.rackings_name, "title")
    _add_text(
        root,
        34,
        65,
        tr(
            "schematics.variant_subtitle",
            "Variant {variant} · used in {count} location(s)",
        ).format(variant=variant.variant_number, count=len(variant.sources)),
        "subtitle",
    )
    _add_text(
        root,
        panel_start_x,
        88,
        tr("schematics.racking", "Racking / PV strings"),
        "subtitle",
    )

    for (
        section,
        section_top,
        positions,
        panel_y,
    ) in section_layouts:
        section_group = _svg_element(
            root,
            "g",
            {
                "class": "product-section",
                "data-preset": section.preset_name,
                "data-product-type": section.product_type,
            },
        )
        _add_text(
            section_group,
            34,
            section_top + 22,
            f"Preset {section.preset_name} · {section.product_type}",
            "section-label",
        )
        _svg_element(
            section_group,
            "line",
            {
                "class": "section-rule",
                "x1": 34,
                "y1": section_top + 34,
                "x2": canvas_width - 34,
                "y2": section_top + 34,
            },
        )
        for string, (head, _tail) in zip(section.strings, positions):
            _draw_string(section_group, string, head, panel_y)
        if not section.strings:
            _add_text(
                section_group,
                panel_start_x,
                section_top + 62,
                section.warning
                or tr("schematics.no_strings", "No strings to draw"),
                "warning-line",
            )
        _draw_harness(
            section_group,
            section,
            positions,
            "POS",
            section_top + _POSITIVE_LANE_OFFSET,
            panel_y,
            _EXTERNAL_ORIGIN_X,
            34.0,
        )
        _draw_harness(
            section_group,
            section,
            positions,
            "NEG",
            section_top + _NEGATIVE_LANE_OFFSET,
            panel_y,
            _EXTERNAL_ORIGIN_X,
            34.0,
        )

    if not variant.sections:
        _add_text(
            root,
            34,
            135,
            tr(
                "schematics.no_products",
                "No drawable IBEX or Homerun products.",
            ),
            "warning-line",
        )

    warning_y = canvas_height - warning_height
    if variant.warnings:
        _add_text(
            root,
            34,
            warning_y,
            tr("schematics.warnings", "Warnings"),
            "warnings-title",
        )
        for index, warning in enumerate(variant.warnings, start=1):
            _add_text(
                root,
                34,
                warning_y + index * 19,
                f"• {warning}",
                "warning-line",
            )

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(value: str) -> str:
    sanitized = _INVALID_FILENAME_CHARS.sub("_", value).strip(" .")
    return sanitized or "racking"


def _managed_manifest_path(directory: Path) -> Path:
    return directory / ".schematics-manifest.json"


def _load_managed_files(directory: Path) -> tuple[str, ...]:
    manifest = _managed_manifest_path(directory)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return ()
    files = data.get("files", []) if isinstance(data, dict) else []
    return tuple(name for name in files if isinstance(name, str))


def _remove_stale_managed_files(directory: Path, keep: set[str]) -> None:
    for name in _load_managed_files(directory):
        if name in keep or Path(name).name != name or not name.lower().endswith(".svg"):
            continue
        candidate = directory / name
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            # A stale file being locked should not prevent writing the new set.
            continue


def _write_atomic(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _filenames_for_variants(
    variants: list[SchematicVariant],
) -> list[str]:
    filenames: list[str] = []
    used: set[str] = set()
    for variant in variants:
        safe = _safe_filename(variant.rackings_name)
        name = f"{safe}__v{variant.variant_number:02d}.svg"
        folded = name.casefold()
        if folded in used:
            digest = hashlib.sha1(
                variant.rackings_name.encode("utf-8")
            ).hexdigest()[:8]
            name = f"{safe}__{digest}__v{variant.variant_number:02d}.svg"
            folded = name.casefold()
        used.add(folded)
        filenames.append(name)
    return filenames


def export_schematics(
    result: ProductGenerationResult,
    project: Project,
    directory: str,
    progress_callback: Callable[[int, int], None] | None = None,
) -> SchematicExportResult:
    """Export one SVG per deduplicated concrete racking variant."""
    output_directory = Path(directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    variants = build_schematic_variants(result, project)
    filenames = _filenames_for_variants(variants)
    rendered = [_render_svg(variant) for variant in variants]
    keep = set(filenames)
    _remove_stale_managed_files(output_directory, keep)

    paths: list[str] = []
    total = len(variants)
    for index, (filename, content) in enumerate(
        zip(filenames, rendered), start=1
    ):
        path = output_directory / filename
        _write_atomic(path, content)
        paths.append(str(path))
        if progress_callback is not None:
            progress_callback(index, total)

    manifest = {
        "version": 1,
        "files": filenames,
    }
    _write_atomic(
        _managed_manifest_path(output_directory),
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    warnings = tuple(
        f"{variant.rackings_name} v{variant.variant_number}: {warning}"
        for variant in variants
        for warning in variant.warnings
    )
    return SchematicExportResult(
        directory=str(output_directory),
        paths=tuple(paths),
        variant_count=len(paths),
        warnings=warnings,
    )


__all__ = [
    "MODULE_COLLAPSE_THRESHOLD",
    "SchematicExportResult",
    "SchematicSection",
    "SchematicString",
    "SchematicVariant",
    "build_schematic_variants",
    "export_schematics",
]
