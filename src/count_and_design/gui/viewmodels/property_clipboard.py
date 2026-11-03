"""Clipboard support for copying dataclass properties between named objects."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, is_dataclass
from typing import Any


class PropertyClipboard:
    """Store a deep copy of every dataclass field except ``name``.

    Fields are discovered at copy time so newly added model fields are included
    automatically.  A clipboard is intentionally type-specific: properties can
    only be pasted into another instance of the copied object's class.
    """

    def __init__(self) -> None:
        self._object_type: type[Any] | None = None
        self._values: dict[str, Any] | None = None

    def copy_from(self, source: object) -> bool:
        if not is_dataclass(source) or isinstance(source, type):
            return False
        self._object_type = type(source)
        self._values = {
            field.name: deepcopy(getattr(source, field.name))
            for field in fields(source)
            if field.name != "name"
        }
        return True

    def paste_into(self, target: object) -> bool:
        if self._values is None or type(target) is not self._object_type:
            return False
        for field_name, value in self._values.items():
            setattr(target, field_name, deepcopy(value))
        return True
