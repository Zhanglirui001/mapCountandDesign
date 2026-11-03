"""Validation helpers."""

from __future__ import annotations

from ...i18n import tr


class ValidationService:
    """Centralizes lightweight validation and normalization rules."""

    @staticmethod
    def normalize_rackings_name(value: str) -> str:
        return value.strip()

    @staticmethod
    def validate_rackings_name(value: str) -> str | None:
        if not value:
            return tr("validation.name_cannot_be_empty", "Name cannot be empty")
        return None
