"""Git-inspired version manager with an operation tree and typed patches.

Each operation creates a :class:`VersionNode` in a tree whose edges are
stored as reversible patches between consecutive states. Structurally changed
lists are replaced atomically so ordering never depends on shifting indexes.
Users can navigate to any unlocked node, and new operations committed after
navigation automatically create a branch.
"""

from __future__ import annotations

import functools
import copy
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from ..models import Project

_F = TypeVar("_F", bound=Callable)


PathPart = str | int


@dataclass(frozen=True)
class PatchOp:
    """A typed, JSON-serializable history patch operation."""

    op: str
    path: tuple[PathPart, ...]
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"op": self.op, "path": list(self.path)}
        if self.op != "remove":
            data["value"] = copy.deepcopy(self.value)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PatchOp":
        op = str(data["op"])
        if op not in {"add", "replace", "remove"}:
            raise ValueError(f"Unsupported patch operation: {op}")
        path = tuple(data.get("path", ()))
        if not all(isinstance(part, (str, int)) for part in path):
            raise ValueError("Patch paths may contain only strings and integers")
        return cls(op=op, path=path, value=copy.deepcopy(data.get("value")))


def _list_identity(values: list[Any]) -> tuple[tuple[str, str], ...] | None:
    """Return a unique, stable identity sequence for model-object lists."""

    identities: list[tuple[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            return None
        if value.get("id") not in (None, ""):
            identity = ("id", str(value["id"]))
        elif value.get("name") not in (None, ""):
            identity = ("name", str(value["name"]))
        else:
            return None
        identities.append(identity)
    if len(set(identities)) != len(identities):
        return None
    return tuple(identities)


def _compute_patch(old: Any, new: Any) -> list[PatchOp]:
    """Compute a typed patch, replacing structurally changed lists atomically."""

    patch: list[PatchOp] = []

    def _walk(o: Any, n: Any, path: tuple[PathPart, ...]) -> None:
        if isinstance(o, dict) and isinstance(n, dict):
            for key in sorted(set(o) | set(n)):
                child_path = (*path, key)
                if key not in o:
                    patch.append(PatchOp("add", child_path, copy.deepcopy(n[key])))
                elif key not in n:
                    patch.append(PatchOp("remove", child_path))
                else:
                    _walk(o[key], n[key], child_path)
            return

        if isinstance(o, list) and isinstance(n, list):
            if o == n:
                return
            old_identity = _list_identity(o)
            new_identity = _list_identity(n)
            if old_identity is not None and old_identity == new_identity:
                for index, (old_item, new_item) in enumerate(zip(o, n)):
                    _walk(old_item, new_item, (*path, index))
            else:
                patch.append(PatchOp("replace", path, copy.deepcopy(n)))
            return

        if o != n:
            patch.append(PatchOp("replace", path, copy.deepcopy(n)))

    _walk(old, new, ())
    return patch


def _apply_patch(target: dict[str, Any], patch: list[PatchOp]) -> None:
    """Apply typed patch operations to *target* in place."""

    for operation in patch:
        if not operation.path:
            if operation.op != "replace" or not isinstance(operation.value, dict):
                raise ValueError("Only a dictionary replacement is valid at the root")
            target.clear()
            target.update(copy.deepcopy(operation.value))
            continue

        container: Any = target
        for part in operation.path[:-1]:
            if isinstance(container, dict) and isinstance(part, str):
                container = container[part]
            elif isinstance(container, list) and isinstance(part, int):
                container = container[part]
            else:
                raise ValueError(f"Invalid patch path: {operation.path!r}")

        leaf = operation.path[-1]
        if isinstance(container, dict) and isinstance(leaf, str):
            if operation.op == "remove":
                container.pop(leaf, None)
            else:
                container[leaf] = copy.deepcopy(operation.value)
        elif isinstance(container, list) and isinstance(leaf, int):
            if operation.op == "remove":
                raise ValueError("List removals must be represented as atomic replacements")
            if leaf >= len(container):
                raise ValueError(f"Patch list index out of range: {operation.path!r}")
            container[leaf] = copy.deepcopy(operation.value)
        else:
            raise ValueError(f"Invalid patch path: {operation.path!r}")


# ============================================================================
# Diff utilities
# ============================================================================


def _compute_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Compute a forward diff from *old* to *new*.

    The diff is a flat dict mapping dotted paths to values that changed,
    were added, or were removed (value is ``None`` sentinel for removals).
    """

    diff: dict[str, Any] = {}

    def _diff(o: Any, n: Any, prefix: str) -> None:
        if isinstance(o, dict) and isinstance(n, dict):
            all_keys = set(o.keys()) | set(n.keys())
            for k in sorted(all_keys):
                path = f"{prefix}.{k}" if prefix else k
                if k not in o:
                    diff[path] = n[k]
                elif k not in n:
                    diff[path] = None  # removed
                else:
                    _diff(o[k], n[k], path)
        elif isinstance(o, list) and isinstance(n, list):
            max_len = max(len(o), len(n))
            for i in range(max_len):
                path = f"{prefix}.{i}"
                if i >= len(o):
                    diff[path] = n[i]
                elif i >= len(n):
                    diff[path] = None
                else:
                    _diff(o[i], n[i], path)
        elif o != n:
            diff[prefix] = n

    _diff(old, new, "")
    return diff


def _apply_diff(target: dict[str, Any], diff: dict[str, Any]) -> None:
    """Apply a forward diff *in-place* to *target*.

    A value of ``None`` for a path means the key should be deleted.
    """

    for path, value in diff.items():
        if path == "__removed__":
            continue
        parts = path.split(".")
        _apply_path(target, parts, value)


def _apply_path(obj: Any, parts: list[str], value: Any) -> None:
    """Set/delete *value* at the dotted path *parts* inside *obj*."""
    if not parts:
        return

    key = parts[0]
    rest = parts[1:]

    if not rest:
        if isinstance(obj, dict):
            if value is None:
                obj.pop(key, None)
            else:
                obj[key] = value
        elif isinstance(obj, list):
            idx = int(key)
            if value is None:
                if idx < len(obj):
                    obj.pop(idx)
            elif idx < len(obj):
                obj[idx] = value
            else:
                while len(obj) < idx:
                    obj.append(None)
                obj.append(value)
        return

    if isinstance(obj, dict):
        if key not in obj or obj[key] is None:
            try:
                int(rest[0])
                is_list = True
            except ValueError:
                is_list = False
            obj[key] = [] if is_list else {}
        _apply_path(obj[key], rest, value)
    elif isinstance(obj, list):
        idx = int(key)
        while len(obj) <= idx:
            obj.append({} if not rest or not _is_int(rest[0]) else [])
        _apply_path(obj[idx], rest, value)


def _is_int(s: str) -> bool:
    try:
        int(s)
        return True
    except ValueError:
        return False


def _invert_diff(forward: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """Compute the reverse of *forward* by reading old values from *old*."""
    reverse: dict[str, Any] = {}
    for path, new_val in forward.items():
        if path == "__removed__":
            continue
        parts = path.split(".")
        old_val = _get_path(old, parts)
        reverse[path] = old_val
    return reverse


def _get_path(obj: Any, parts: list[str]) -> Any:
    """Navigate *parts* into *obj*, returning the value or None."""
    for part in parts:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list):
            idx = int(part)
            if idx < len(obj):
                obj = obj[idx]
            else:
                return None
        else:
            return None
    return obj


# ============================================================================
# Diff Interpreter — generates human-readable summaries from diffs
# ============================================================================

# Path prefixes that identify structural (add/delete) operations
_STRUCTURAL_ROOTS = {
    "invs": ("INV", "invs"),
    "product_presets": ("ProductPreset", "product_presets"),
    "rackings_list": ("Rackings", "rackings_list"),
    "length_identifiers": ("LengthIdentifier", "length_identifiers"),
}

# Field-name → i18n key
_FIELD_LABEL_KEYS: dict[str, str] = {
    "name": "diff.field.name",
    "address": "diff.field.address",
    "client_name": "diff.field.client_name",
    "project_type": "diff.field.project_type",
    "racking_pitch": "diff.field.racking_pitch",
    "lynx_preset": "diff.field.lynx_preset",
    "cluster_length": "diff.field.cluster_length",
    "box_position": "diff.field.box_position",
    "offset": "diff.field.offset",
    "rackings": "diff.field.rackings",
    "product_presets": "diff.field.product_presets",
    "length": "diff.field.length",
    "branch_count": "diff.field.branch_count",
    "product_type": "diff.field.product_type",
    "pv_panel": "diff.field.pv_panel",
}


def _label_for_field(field: str, tr: Callable[[str, str], str] | None = None) -> str:
    key = _FIELD_LABEL_KEYS.get(field)
    if key and tr:
        return tr(key, field)
    return field


def _extract_name(item: Any) -> str | None:
    """Extract a 'name' field from a dict item if possible."""
    if isinstance(item, dict):
        return item.get("name")
    return None


def _resolve_ancestor_names(
    snapshot: dict[str, Any],
    parts: list[str],
    tr: Callable[[str, str], str] | None = None,
) -> str:
    """Build a context prefix by looking up real names from *snapshot*.

    For a path like ``invs.2.nodes.1.rows.3.name`` this walks the
    snapshot at each structural level and returns something like
    ``"INV3 Node2 Row4 "`` (using actual names found in the data).

    When a name cannot be resolved the index-based i18n fallback is used.
    """
    def _fmt(key: str, index: int) -> str:
        tpl = tr(key, key) if tr else key
        return tpl.format(index=index)

    context_parts: list[str] = []

    # INV level: parts[0] == "invs", parts[1] == inv_index
    if len(parts) >= 2 and parts[0] == "invs":
        inv_idx = int(parts[1])
        inv_container = snapshot.get("invs")
        inv_name: str | None = None
        if isinstance(inv_container, list) and inv_idx < len(inv_container):
            inv_name = _extract_name(inv_container[inv_idx])
        context_parts.append(inv_name or _fmt("diff.structural.inv", inv_idx + 1))

        # Node level: parts[2] == "nodes", parts[3] == node_index
        if len(parts) >= 4 and parts[2] == "nodes":
            node_idx = int(parts[3])
            node_name: str | None = None
            if isinstance(inv_container, list) and inv_idx < len(inv_container):
                inv_item = inv_container[inv_idx]
                if isinstance(inv_item, dict):
                    nodes = inv_item.get("nodes")
                    if isinstance(nodes, list) and node_idx < len(nodes):
                        node_name = _extract_name(nodes[node_idx])
            context_parts.append(node_name or _fmt("diff.structural.node", node_idx + 1))

            # Row level: parts[4] == "rows", parts[5] == row_index
            if len(parts) >= 6 and parts[4] == "rows":
                row_idx = int(parts[5])
                row_name: str | None = None
                if isinstance(inv_container, list) and inv_idx < len(inv_container):
                    inv_item = inv_container[inv_idx]
                    if isinstance(inv_item, dict):
                        nodes = inv_item.get("nodes")
                        if isinstance(nodes, list) and node_idx < len(nodes):
                            node_item = nodes[node_idx]
                            if isinstance(node_item, dict):
                                rows = node_item.get("rows")
                                if isinstance(rows, list) and row_idx < len(rows):
                                    row_name = _extract_name(rows[row_idx])
                context_parts.append(row_name or _fmt("diff.structural.row", row_idx + 1))

                # Racking entry: parts[6] == "racking_entries", parts[7] == entry_index
                if len(parts) >= 8 and parts[6] == "racking_entries":
                    entry_idx = int(parts[7]) + 1
                    context_parts.append(_fmt("diff.structural.racking", entry_idx))

    elif len(parts) >= 2 and parts[0] == "product_presets":
        pp_idx = int(parts[1])
        pp_container = snapshot.get("product_presets")
        pp_name: str | None = None
        if isinstance(pp_container, list) and pp_idx < len(pp_container):
            pp_name = _extract_name(pp_container[pp_idx])
        context_parts.append(pp_name or _fmt("diff.structural.pp", pp_idx + 1))

    elif len(parts) >= 2 and parts[0] == "rackings_list":
        r_idx = int(parts[1])
        r_container = snapshot.get("rackings_list")
        r_name: str | None = None
        if isinstance(r_container, list) and r_idx < len(r_container):
            r_name = _extract_name(r_container[r_idx])
        context_parts.append(r_name or _fmt("diff.structural.rackings_short", r_idx + 1))

    elif len(parts) >= 2 and parts[0] == "length_identifiers":
        li_idx = int(parts[1])
        li_container = snapshot.get("length_identifiers")
        li_name: str | None = None
        if isinstance(li_container, list) and li_idx < len(li_container):
            li_name = _extract_name(li_container[li_idx])
        context_parts.append(li_name or _fmt("diff.structural.li", li_idx + 1))

    if context_parts:
        return " ".join(context_parts) + " "
    return ""


def _describe_value(val: Any, tr: Callable[[str, str], str] | None = None) -> str:
    """Brief string representation of a value."""
    if val is None:
        return tr("diff.value.empty", "(empty)") if tr else "(empty)"
    if isinstance(val, dict):
        name = _extract_name(val)
        return f'"{name}"' if name else (tr("diff.value.object", "(object)") if tr else "(object)")
    if isinstance(val, list):
        tpl = tr("diff.value.items", "{count} item(s)") if tr else "{count} item(s)"
        return tpl.format(count=len(val))
    if isinstance(val, bool):
        return (tr("diff.value.yes", "Yes") if tr else "Yes") if val else (tr("diff.value.no", "No") if tr else "No")
    s = str(val)
    if len(s) > 40:
        s = s[:37] + "..."
    return s


def _find_ancestor_name(snapshot: dict[str, Any], path: str, levels_up: int = 0) -> str | None:
    """Walk up *levels_up* path segments then look for the 'name' field
    of the container at that level."""
    parts = path.split(".")
    if levels_up >= len(parts):
        return None
    target_parts = parts[:len(parts) - levels_up]
    container = _get_path(snapshot, target_parts)
    if isinstance(container, dict):
        return container.get("name")
    return None


class DiffInterpreter:
    """Generates a short, human-readable summary from a diff."""

    @staticmethod
    def summarize(
        diff_forward: dict[str, Any],
        pre_snapshot: dict[str, Any],
        post_snapshot: dict[str, Any],
        category: str,
        tr: Callable[[str, str], str] | None = None,
    ) -> str:
        """Return a one-line summary of what changed.

        Falls back to *category* when the diff cannot be interpreted.
        """
        paths = sorted(diff_forward.keys())

        # -- detect structural changes first (add / delete of whole objects) --

        additions: list[str] = []
        deletions: list[str] = []

        for path in paths:
            old_val = _get_path(pre_snapshot, path.split("."))
            new_val = diff_forward[path]

            # Addition: path didn't exist before and value is a dict
            if old_val is None and isinstance(new_val, dict):
                label = DiffInterpreter._structural_label(path, new_val, "added", tr)
                if label:
                    additions.append(label)

            # Deletion: path was a dict before and new value is None
            if isinstance(old_val, dict) and new_val is None:
                label = DiffInterpreter._structural_label(path, old_val, "removed", tr)
                if label:
                    deletions.append(label)

        if deletions:
            tpl = tr("diff.summary.deleted", "Deleted {items}") if tr else "Deleted {items}"
            return tpl.format(items="、".join(deletions[:3]))
        if additions:
            tpl = tr("diff.summary.added", "Added {items}") if tr else "Added {items}"
            return tpl.format(items="、".join(additions[:3]))

        # -- detect field modifications --

        modifications: list[str] = []
        for path in paths:
            old_val = _get_path(pre_snapshot, path.split("."))
            new_val = diff_forward[path]

            # Skip structural (already handled) and unchanged
            if isinstance(old_val, dict) or isinstance(new_val, dict):
                continue
            if isinstance(old_val, list) or isinstance(new_val, list):
                continue
            if old_val is None and new_val is None:
                continue

            mod = DiffInterpreter._field_change(path, old_val, new_val, pre_snapshot, tr)
            if mod:
                modifications.append(mod)

        if modifications:
            return "、".join(modifications[:4])
            # Keep it short — up to ~4 changes

        # Fallback: just report the category and count of paths changed
        if len(paths) == 1:
            field = paths[0].rsplit(".", 1)[-1]
            tpl = tr("diff.summary.changed", "{field} changed") if tr else "{field} changed"
            return tpl.format(field=_label_for_field(field, tr))
        tpl = tr("diff.summary.multiple_changes", "{count} change(s)") if tr else "{count} change(s)"
        return tpl.format(count=len(paths))

    @staticmethod
    def _structural_label(path: str, item: dict, mode: str, tr: Callable[[str, str], str] | None = None) -> str | None:
        """Generate a label like 'INV3' or 'INV3/Node2' for a structural change."""
        parts = path.split(".")

        def _fmt(key: str, index: int) -> str:
            tpl = tr(key, key) if tr else key
            return tpl.format(index=index)

        # Top-level INV: invs.I
        if len(parts) == 2 and parts[0] == "invs":
            inv_name = _extract_name(item) or _fmt("diff.structural.inv", int(parts[1]) + 1)
            return inv_name

        # INV-level child (node): invs.I.nodes.J
        if len(parts) >= 4 and parts[0] == "invs" and parts[2] == "nodes":
            inv_idx = int(parts[1]) + 1
            node_idx = int(parts[3]) + 1
            node_name = _extract_name(item) or _fmt("diff.structural.node", node_idx)
            return f"{_fmt('diff.structural.inv', inv_idx)}/{node_name}"

        # Row: invs.I.nodes.J.rows.K
        if len(parts) >= 6 and parts[0] == "invs" and parts[2] == "nodes" and parts[4] == "rows":
            inv_idx = int(parts[1]) + 1
            node_idx = int(parts[3]) + 1
            row_idx = int(parts[5]) + 1
            row_name = _extract_name(item) or _fmt("diff.structural.row", row_idx)
            return f"{_fmt('diff.structural.inv', inv_idx)}/{_fmt('diff.structural.node', node_idx)}/{row_name}"

        # Racking entry: invs.I.nodes.J.rows.K.racking_entries.E
        if len(parts) >= 8 and parts[0] == "invs" and parts[6] == "racking_entries":
            row_idx = int(parts[5]) + 1
            entry_idx = int(parts[7]) + 1
            return f"{_fmt('diff.structural.row', row_idx)} {_fmt('diff.structural.racking', entry_idx)}"

        # ProductPreset: product_presets.I
        if len(parts) == 2 and parts[0] == "product_presets":
            pp_name = _extract_name(item) or _fmt("diff.structural.pp", int(parts[1]) + 1)
            return pp_name

        # Rackings: rackings_list.I
        if len(parts) == 2 and parts[0] == "rackings_list":
            r_name = _extract_name(item) or _fmt("diff.structural.rackings_short", int(parts[1]) + 1)
            return r_name

        # LengthIdentifier: length_identifiers.I
        if len(parts) == 2 and parts[0] == "length_identifiers":
            li_name = _extract_name(item) or _fmt("diff.structural.li", int(parts[1]) + 1)
            return li_name

        return None

    @staticmethod
    def _field_change(
        path: str,
        old_val: Any,
        new_val: Any,
        pre_snapshot: dict[str, Any],
        tr: Callable[[str, str], str] | None = None,
    ) -> str | None:
        """Describe a field-level change like 'Name → "Foo"'."""
        parts = path.split(".")
        field = parts[-1]
        label = _label_for_field(field, tr)

        # Look up real ancestor names from the snapshot
        context = _resolve_ancestor_names(pre_snapshot, parts, tr)

        old_str = _describe_value(old_val, tr)
        new_str = _describe_value(new_val, tr)

        if old_val is None:
            tpl = tr("diff.change.set_to", "{context}{label} set to {value}") if tr else "{context}{label} set to {value}"
            return tpl.format(context=context, label=label, value=new_str)
        if new_val is None:
            tpl = tr("diff.change.cleared", "{context}{label} cleared") if tr else "{context}{label} cleared"
            return tpl.format(context=context, label=label)
        tpl = tr("diff.change.modified", "{context}{label}: {old}→{new}") if tr else "{context}{label}: {old}→{new}"
        return tpl.format(context=context, label=label, old=old_str, new=new_str)


# ============================================================================
# Version Node
# ============================================================================


class VersionNode:
    """A single node in the operation tree."""

    __slots__ = (
        "id", "description", "summary", "parent_id", "children_ids",
        "timestamp", "patch_forward", "patch_reverse", "metadata", "locked",
        "diff_forward", "diff_reverse",
    )

    def __init__(
        self,
        node_id: str,
        description: str,
        parent_id: str | None,
        summary: str = "",
        timestamp: float | None = None,
        patch_forward: list[PatchOp] | None = None,
        patch_reverse: list[PatchOp] | None = None,
        metadata: dict[str, Any] | None = None,
        locked: bool = False,
        diff_forward: dict[str, Any] | None = None,
        diff_reverse: dict[str, Any] | None = None,
    ) -> None:
        self.id = node_id
        self.description = description
        self.summary = summary
        self.parent_id = parent_id
        self.children_ids: list[str] = []
        self.timestamp = timestamp if timestamp is not None else time.time()
        self.patch_forward = patch_forward
        self.patch_reverse = patch_reverse
        self.metadata = copy.deepcopy(metadata or {})
        self.locked = locked
        self.diff_forward: dict[str, Any] = diff_forward or {}
        self.diff_reverse: dict[str, Any] = diff_reverse or {}

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "description": self.description,
            "summary": self.summary,
            "parent_id": self.parent_id,
            "children_ids": self.children_ids,
            "timestamp": self.timestamp,
            "metadata": copy.deepcopy(self.metadata),
            "locked": self.locked,
        }
        if self.patch_forward is not None and self.patch_reverse is not None:
            data["patch_forward"] = [op.to_dict() for op in self.patch_forward]
            data["patch_reverse"] = [op.to_dict() for op in self.patch_reverse]
        else:
            data["diff_forward"] = self.diff_forward
            data["diff_reverse"] = self.diff_reverse
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VersionNode":
        patch_forward_data = data.get("patch_forward")
        patch_reverse_data = data.get("patch_reverse")
        return cls(
            node_id=data["id"],
            description=data["description"],
            parent_id=data.get("parent_id"),
            summary=data.get("summary", ""),
            timestamp=data.get("timestamp"),
            patch_forward=(
                [PatchOp.from_dict(item) for item in patch_forward_data]
                if isinstance(patch_forward_data, list) else None
            ),
            patch_reverse=(
                [PatchOp.from_dict(item) for item in patch_reverse_data]
                if isinstance(patch_reverse_data, list) else None
            ),
            metadata=data.get("metadata", {}),
            locked=bool(data.get("locked", False)),
            diff_forward=data.get("diff_forward", {}),
            diff_reverse=data.get("diff_reverse", {}),
        )


# ============================================================================
# Version Manager
# ============================================================================


class VersionManager:
    """Manages an operation tree with patch-based versioning.

    Usage inside a ViewModel method::

        @versionable("Rename INV")
        def update_inv_field(self, field_name: str, value: str) -> None:
            ...

        The decorator takes a snapshot before the method executes and computes
        a reversible patch afterward, creating a new node in the operation tree.
    """

    def __init__(self, translator: Callable[[str, str], str] | None = None) -> None:
        root = VersionNode(
            node_id="_root",
            description="version.initial_state",
            parent_id=None,
            summary="",
            patch_forward=[],
            patch_reverse=[],
        )
        self._nodes: dict[str, VersionNode] = {root.id: root}
        self._root_id = root.id
        self._current_id = root.id
        self._translator: Callable[[str, str], str] | None = translator

    # -- queries ---------------------------------------------------------

    @property
    def root_id(self) -> str:
        return self._root_id

    @property
    def current_id(self) -> str:
        return self._current_id

    @property
    def current_description(self) -> str:
        node = self._nodes.get(self._current_id)
        return node.description if node else ""

    @property
    def current_summary(self) -> str:
        node = self._nodes.get(self._current_id)
        return node.summary if node else ""

    def get_node(self, node_id: str) -> VersionNode | None:
        return self._nodes.get(node_id)

    def can_navigate_to(self, node_id: str) -> bool:
        """Return True if *node_id* exists and is not the current node."""
        node = self._nodes.get(node_id)
        return node is not None and node_id != self._current_id and not node.locked

    def lock_existing_history(self) -> None:
        """Lock every loaded node except the current open-state boundary."""
        for node in self._nodes.values():
            node.locked = node.id != self._current_id

    def get_tree(self) -> list[dict[str, Any]]:
        """Return the operation tree as a list of node dicts for display.

        Each dict contains: id, description, summary, parent_id,
        timestamp, is_current, children.

        Uses an explicit stack so that arbitrarily deep trees never
        overflow the C call stack.
        """
        root_id = self._root_id
        built: dict[str, dict[str, Any]] = {}
        # Each stack entry is (node_id, state): 0 = first visit, 1 = children ready
        stack: list[tuple[str, int]] = [(root_id, 0)]

        while stack:
            node_id, state = stack.pop()

            if state == 0:
                stack.append((node_id, 1))
                node = self._nodes[node_id]
                # Reverse so that original order is preserved on pop
                for child_id in reversed(node.children_ids):
                    stack.append((child_id, 0))
            else:
                node = self._nodes[node_id]
                children = [built[cid] for cid in node.children_ids]
                built[node_id] = {
                    "id": node.id,
                    "description": node.description,
                    "summary": node.summary,
                    "parent_id": node.parent_id,
                    "timestamp": node.timestamp,
                    "is_current": node.id == self._current_id,
                    "is_locked": node.locked,
                    "children": children,
                }

        return [built[root_id]]

    def get_path_to_root(self, node_id: str) -> list[str]:
        """Return the list of node IDs from *node_id* up to root (inclusive)."""
        path: list[str] = []
        current = node_id
        while current is not None:
            path.append(current)
            node = self._nodes.get(current)
            current = node.parent_id if node else None
        return path

    def get_path_set(self, node_id: str) -> set[str]:
        """Return the set of node IDs on the path from *node_id* to root."""
        return set(self.get_path_to_root(node_id))

    # -- operations -------------------------------------------------------

    def commit(
        self,
        pre_snapshot: dict[str, Any],
        post_project: Project,
        description: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a new version node as a child of the current node.

        *pre_snapshot* must be the ``project.to_dict()`` taken **before**
        the modification.  *post_project* is the project **after** the
        modification.

        Returns the new node ID.
        """
        post_snapshot = post_project.to_dict()

        pre_data = {k: v for k, v in pre_snapshot.items()
                     if k not in ("schema_version", "app_version")}
        post_data = {k: v for k, v in post_snapshot.items()
                      if k not in ("schema_version", "app_version")}

        patch_fwd = _compute_patch(pre_data, post_data)
        if not patch_fwd:
            return self._current_id

        patch_rev = _compute_patch(post_data, pre_data)

        summary = self._summarize_operation(
            metadata,
            _compute_diff(pre_data, post_data),
            pre_data,
            post_data,
            description,
        )

        node_id = str(uuid.uuid4())[:8]
        node = VersionNode(
            node_id=node_id,
            description=description,
            summary=summary,
            parent_id=self._current_id,
            patch_forward=patch_fwd,
            patch_reverse=patch_rev,
            metadata=metadata,
        )
        self._nodes[node_id] = node
        parent = self._nodes[self._current_id]
        parent.children_ids.append(node_id)
        self._current_id = node_id
        return node_id

    def _summarize_operation(
        self,
        metadata: dict[str, Any] | None,
        legacy_diff: dict[str, Any],
        pre_data: dict[str, Any],
        post_data: dict[str, Any],
        description: str,
    ) -> str:
        metadata = metadata or {}
        kind = metadata.get("kind")
        subjects = [str(item) for item in metadata.get("subjects", ()) if str(item)]
        if kind in {"sort", "reorder"} and subjects:
            shown = subjects[:4]
            items = "、".join(shown)
            if len(subjects) > len(shown):
                items += "…"
            if kind == "reorder":
                template = (
                    self._translator("history.summary.reorder", "Reordered {items}")
                    if self._translator else "Reordered {items}"
                )
                return template.format(items=items)
            direction_key = "ordering.descending" if metadata.get("descending") else "ordering.ascending"
            direction = (
                self._translator(direction_key, "Descending" if metadata.get("descending") else "Ascending")
                if self._translator else ("Descending" if metadata.get("descending") else "Ascending")
            )
            field = str(metadata.get("field", ""))
            field_label = (
                self._translator(f"ordering.{field}", field) if self._translator and field else field
            )
            template = (
                self._translator(
                    "history.summary.sort",
                    "Sorted {items} by {field} ({direction})",
                )
                if self._translator else "Sorted {items} by {field} ({direction})"
            )
            return template.format(items=items, field=field_label, direction=direction)
        return DiffInterpreter.summarize(
            legacy_diff,
            pre_data,
            post_data,
            description,
            self._translator,
        )

    def checkout(self, node_id: str, project: Project) -> Project:
        """Navigate from the current node to *node_id*, returning a new
        :class:`Project` that reflects the target state.

        Walks the tree: up from current to the lowest common ancestor,
        then down to target, applying reverse and forward diffs.
        """
        if node_id == self._current_id:
            return project

        if node_id not in self._nodes:
            raise ValueError(f"Unknown node: {node_id}")
        if self._nodes[node_id].locked:
            raise ValueError(f"History node is read-only: {node_id}")

        path_from_current = self.get_path_to_root(self._current_id)
        path_from_target = self.get_path_to_root(node_id)

        current_set = set(path_from_current)
        lca = None
        for nid in path_from_target:
            if nid in current_set:
                lca = nid
                break

        if lca is None:
            raise ValueError("No common ancestor – tree is disconnected")

        current_dict = project.to_dict()
        data = {k: v for k, v in current_dict.items()
                if k not in ("schema_version", "app_version")}

        for nid in path_from_current:
            if nid == lca:
                break
            node = self._nodes[nid]
            if node.patch_reverse is not None:
                _apply_patch(data, node.patch_reverse)
            else:
                _apply_diff(data, node.diff_reverse)

        lca_idx = path_from_target.index(lca)
        down_path = list(reversed(path_from_target[:lca_idx]))

        for nid in down_path:
            node = self._nodes[nid]
            if node.locked:
                raise ValueError(f"History node is read-only: {nid}")
            if node.patch_forward is not None:
                _apply_patch(data, node.patch_forward)
            else:
                _apply_diff(data, node.diff_forward)

        data["schema_version"] = current_dict.get("schema_version", 2)
        data["app_version"] = current_dict.get("app_version", "")
        new_project = Project.from_dict(data)
        self._current_id = node_id
        return new_project

    def clear(self) -> None:
        """Reset to a single root node (e.g. after loading a new project)."""
        root = VersionNode(
            node_id="_root",
            description="version.initial_state",
            parent_id=None,
            summary="",
            patch_forward=[],
            patch_reverse=[],
        )
        self._nodes = {root.id: root}
        self._root_id = root.id
        self._current_id = root.id

    def set_initial_snapshot(self, project: Project) -> None:
        """Record the current project state as the root node's baseline."""
        self._current_id = self._root_id

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "history_format": 2,
            "root_id": self._root_id,
            "current_id": self._current_id,
            "nodes": {
                nid: node.to_dict() for nid, node in self._nodes.items()
            },
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        translator: Callable[[str, str], str] | None = None,
        *,
        lock_legacy: bool = False,
    ) -> "VersionManager":
        vm = cls.__new__(cls)
        vm._nodes = {}
        for nid, node_data in data.get("nodes", {}).items():
            vm._nodes[nid] = VersionNode.from_dict(node_data)
        vm._root_id = data.get("root_id", "_root")
        vm._current_id = data.get("current_id", "_root")
        vm._translator = translator
        for node in vm._nodes.values():
            node.children_ids = []
        for node in vm._nodes.values():
            if node.parent_id and node.parent_id in vm._nodes:
                parent = vm._nodes[node.parent_id]
                if node.id not in parent.children_ids:
                    parent.children_ids.append(node.id)
        if vm._root_id not in vm._nodes or vm._current_id not in vm._nodes:
            raise ValueError("Version history references an unknown root or current node")
        if lock_legacy and int(data.get("history_format", 1)) < 2:
            vm.lock_existing_history()
        return vm


# ============================================================================
# Decorator
# ============================================================================


def versionable(
    description: str,
    *,
    metadata_factory: Callable[..., dict[str, Any]] | None = None,
) -> Callable[[_F], _F]:
    """Decorator that creates a version node recording the changes made by
    the decorated method.

    The decorated method must belong to an object that has ``_project`` and
    ``_version_manager`` attributes.  If either is missing the decorator is
    a no-op.

    Usage::

        @versionable("修改名称")
        def update_name(self, value: str) -> None:
            ...
    """

    def decorator(func: _F) -> _F:
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            manager = getattr(self, "_version_manager", None)
            project = getattr(self, "_project", None)
            pre_snapshot: dict[str, Any] | None = None
            metadata: dict[str, Any] | None = None
            if manager is not None and project is not None:
                pre_snapshot = project.to_dict()
                if metadata_factory is not None:
                    metadata = metadata_factory(self, *args, **kwargs)
            result = func(self, *args, **kwargs)
            if manager is not None and project is not None and pre_snapshot is not None:
                manager.commit(pre_snapshot, project, description, metadata=metadata)
            on_version_changed = getattr(self, "_on_version_state_changed", None)
            if on_version_changed is not None:
                on_version_changed()
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
