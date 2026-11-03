"""MinerU document parsing provider based on its multipart OpenAPI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import mimetypes
from pathlib import Path
from threading import get_ident, local
from typing import Any

import requests

from count_and_design.application.config import MinerUOcrConfig

from ..parsing import normalize_ocr_content
from ..types import OcrResult, OcrServiceError


MinerUFile = str | Path | bytes


@dataclass(frozen=True)
class MinerUParseResult:
    """Normalized MinerU response with provider-specific data preserved."""

    markdown: str = ""
    pages: int | None = None
    duration: float | None = None
    content_list: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )


class MinerUOcrService:
    """Client for the built-in MinerU synchronous parsing service."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float | None = None,
        connect_timeout: float | None = None,
        backend: str | None = None,
        effort: str | None = None,
        config: MinerUOcrConfig | None = None,
    ) -> None:
        settings = config or (
            MinerUOcrConfig(base_url=base_url)
            if base_url is not None
            else MinerUOcrConfig.from_env()
        )
        self._base_url = (
            base_url or settings.base_url
        ).rstrip("/")
        read_timeout = settings.timeout if timeout is None else timeout
        connect = settings.connect_timeout if connect_timeout is None else connect_timeout
        # Split (connect, read) so an unreachable server fails within the
        # connect budget instead of hanging for the full read timeout.
        self._timeout: tuple[float, float] = (connect, read_timeout)
        self._backend = backend or settings.backend
        self._effort = effort or settings.effort
        self._lang_list = settings.lang_list
        # One pooled session reuses TCP connections across the many parse
        # requests Agent3 issues per run. Worker threads receive independent
        # sessions because requests.Session contains mutable request state.
        self._session = requests.Session()
        self._session_owner = get_ident()
        self._thread_sessions = local()

    def _session_for_current_thread(self) -> requests.Session:
        if get_ident() == self._session_owner:
            return self._session
        session = getattr(self._thread_sessions, "session", None)
        if session is None:
            session = requests.Session()
            self._thread_sessions.session = session
        return session

    @property
    def base_url(self) -> str:
        return self._base_url

    def health_check(self) -> bool:
        try:
            response = self._session_for_current_thread().get(
                f"{self._base_url}/health",
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise OcrServiceError(
                f"MinerU health check failed: {exc}"
            ) from exc
        if not 200 <= response.status_code < 300:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        return (
            isinstance(payload, dict)
            and payload.get("status") in {"ok", "healthy"}
        )

    def recognize(self, image_bytes: bytes) -> OcrResult:
        """Recognize one PNG through `/file_parse`."""

        if not image_bytes:
            raise OcrServiceError("No  image was provided.")
        result = self.parse_file(
            image_bytes,
            filename="screenshot.png",
            parse_method="ocr",
        )
        if not result.markdown.strip():
            raise OcrServiceError(
                "MinerU response did not contain markdown content."
            )
        return normalize_ocr_content(
            result.markdown,
            raw=result.raw,
            provider="mineru",
        )

    def parse_file(
        self,
        file: MinerUFile,
        *,
        filename: str | None = None,
        lang_list: Sequence[str] | None = None,
        backend: str | None = None,
        effort: str | None = None,
        parse_method: str = "auto",
        formula_enable: bool = True,
        table_enable: bool = True,
        image_analysis: bool = True,
        return_content_list: bool = False,
        extra_options: Mapping[str, Any] | None = None,
    ) -> MinerUParseResult:
        """Synchronously parse one supported document or image."""

        name, data, mime_type = _read_file(file, filename=filename)
        form = _build_form(
            lang_list=lang_list or self._lang_list,
            backend=backend or self._backend,
            effort=effort or self._effort,
            parse_method=parse_method,
            formula_enable=formula_enable,
            table_enable=table_enable,
            image_analysis=image_analysis,
            return_content_list=return_content_list,
            extra_options=extra_options,
        )
        try:
            response = self._session_for_current_thread().post(
                f"{self._base_url}/file_parse",
                files=[("files", (name, data, mime_type))],
                data=form,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise OcrServiceError(
                f"MinerU parse request failed: {exc}"
            ) from exc
        if not 200 <= response.status_code < 300:
            detail = response.text.strip()[:500]
            message = (
                f"MinerU server returned HTTP {response.status_code}"
            )
            if detail:
                message = f"{message}: {detail}"
            raise OcrServiceError(message)
        try:
            payload = response.json()
        except ValueError as exc:
            raise OcrServiceError(
                "MinerU server returned invalid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise OcrServiceError(
                "MinerU server returned an unsupported response."
            )
        return _parse_result(payload)


def _read_file(
    file: MinerUFile,
    *,
    filename: str | None,
) -> tuple[str, bytes, str]:
    if isinstance(file, bytes):
        if not file:
            raise OcrServiceError("MinerU input file is empty.")
        name = filename or "document.bin"
        data = file
    else:
        path = Path(file).expanduser()
        if not path.exists():
            raise OcrServiceError(
                f"MinerU input file does not exist: {path}"
            )
        name = filename or path.name
        data = path.read_bytes()
    mime_type = (
        mimetypes.guess_type(name)[0]
        or "application/octet-stream"
    )
    return name, data, mime_type


def _build_form(
    *,
    lang_list: Sequence[str],
    backend: str,
    effort: str,
    parse_method: str,
    formula_enable: bool,
    table_enable: bool,
    image_analysis: bool,
    return_content_list: bool,
    extra_options: Mapping[str, Any] | None,
) -> list[tuple[str, str]]:
    form = [
        ("lang_list", language)
        for language in lang_list
    ]
    form.extend(
        [
            ("backend", backend),
            ("effort", effort),
            ("parse_method", parse_method),
            ("formula_enable", _form_bool(formula_enable)),
            ("table_enable", _form_bool(table_enable)),
            ("image_analysis", _form_bool(image_analysis)),
            ("return_md", "true"),
            (
                "return_content_list",
                _form_bool(return_content_list),
            ),
            ("response_format_zip", "false"),
        ]
    )
    for key, value in (extra_options or {}).items():
        form.append((str(key), _form_value(value)))
    return form


def _form_bool(value: bool) -> str:
    return "true" if value else "false"


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return _form_bool(value)
    return str(value)


def _normalize_content_list(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    return []


def _parse_result(payload: dict[str, Any]) -> MinerUParseResult:
    result_payload = _unwrap_result(payload)
    return MinerUParseResult(
        markdown=str(result_payload.get("md_content") or ""),
        pages=(
            int(result_payload["pages"])
            if isinstance(result_payload.get("pages"), int)
            else None
        ),
        duration=(
            float(result_payload["duration"])
            if isinstance(
                result_payload.get("duration"),
                (int, float),
            )
            else None
        ),
        content_list=_normalize_content_list(
            result_payload.get("content_list")
        ),
        raw=payload,
    )


def _unwrap_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Support both OpenAPI ParseResponse and gateway result envelopes."""

    if "md_content" in payload:
        return payload
    results = payload.get("results")
    if isinstance(results, dict):
        for item in results.values():
            if isinstance(item, dict):
                return item
    return payload
