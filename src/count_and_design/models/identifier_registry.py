from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .length_identifier import LengthIdentifier


@dataclass(eq=False)
class IdentifierRegistry:
    """Stable runtime namespace used to resolve length identifiers.

    The registry deliberately keeps the project's identifier list by
    reference.  Length instances bind to this stable object once and always
    resolve names against its current contents.
    """

    identifiers: list["LengthIdentifier"] = field(default_factory=list)

    def find(self, name: str) -> "LengthIdentifier | None":
        for identifier in self.identifiers:
            if identifier.name == name:
                return identifier
        return None

    def replace_identifiers(
        self, identifiers: list["LengthIdentifier"]
    ) -> None:
        """Point the stable registry at a replacement project list."""
        self.identifiers = identifiers
