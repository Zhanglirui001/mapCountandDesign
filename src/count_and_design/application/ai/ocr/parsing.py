"""Normalize OCR text and convert recognized tables to TSV."""

from __future__ import annotations

from html.parser import HTMLParser
import re
from typing import Any

from .types import OcrResult


def normalize_ocr_content(
    content: str,
    *,
    raw: dict[str, Any] | None = None,
    provider: str = "",
) -> OcrResult:
    cleaned = re.sub(r"<\|[^|]*\|>", "", content).strip()
    rows = _parse_html_table(cleaned) or _parse_markdown_table(cleaned)
    if rows:
        return OcrResult(
            text=_rows_to_tsv(rows),
            is_table=True,
            raw=raw,
            provider=provider,
        )
    return OcrResult(
        text=cleaned,
        is_table=False,
        raw=raw,
        provider=provider,
    )


def _parse_html_table(content: str) -> list[list[str]]:
    if "<table" not in content.lower():
        return []
    parser = _HtmlTableParser()
    try:
        parser.feed(content)
    except Exception:
        return []
    return parser.best_table()


def _parse_markdown_table(content: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "|" not in stripped[1:]:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and all(
            set(cell) <= {"-", ":", " "} and cell
            for cell in cells
        ):
            continue
        rows.append([_clean_cell(cell) for cell in cells])
    return (
        [row for row in rows if any(cell for cell in row)]
        if len(rows) >= 2
        else []
    )


def _rows_to_tsv(rows: list[list[str]]) -> str:
    return "\n".join(
        "\t".join(_clean_cell(cell) for cell in row)
        for row in rows
    ).strip()


def _clean_cell(value: Any) -> str:
    return (
        str(value)
        .replace("\t", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


class _HtmlTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._tables: list[list[list[str]]] = []
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag: str, _attrs: Any) -> None:
        tag = tag.lower()
        if tag == "table":
            self._current_table = []
        elif tag == "tr" and self._current_table is not None:
            self._current_row = []
        elif tag in ("td", "th") and self._current_row is not None:
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if (
            tag in ("td", "th")
            and self._cell_parts is not None
            and self._current_row is not None
        ):
            self._current_row.append(
                "".join(self._cell_parts).strip()
            )
            self._cell_parts = None
        elif (
            tag == "tr"
            and self._current_row is not None
            and self._current_table is not None
        ):
            self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._current_table is not None:
            self._tables.append(self._current_table)
            self._current_table = None

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def best_table(self) -> list[list[str]]:
        best: list[list[str]] = []
        best_score = 0
        for table in self._tables:
            cleaned = [
                row
                for row in table
                if any(cell.strip() for cell in row)
            ]
            score = sum(len(row) for row in cleaned)
            if score > best_score:
                best = cleaned
                best_score = score
        return best
