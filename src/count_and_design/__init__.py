"""Count and Design package."""

__version__ = "1.7.10"

from .models import (
    Inv,
    IdentifierRegistry,
    Length,
    LengthIdentifier,
    Node,
    Project,
    Rackings,
    Row,
    Wire,
)


def main() -> int:
    from .main import main as _main

    return _main()


__all__ = [
    "__version__",
    "main",
    "Inv",
    "IdentifierRegistry",
    "Length",
    "LengthIdentifier",
    "Node",
    "Project",
    "Rackings",
    "Row",
    "Wire",
]
