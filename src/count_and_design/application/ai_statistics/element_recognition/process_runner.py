"""Killable process boundary for production Agent3 execution.

Agent3 mixes blocking HTTP requests with CPU-heavy ONNX inference.  Neither a
``QThread`` interruption request nor ``Future.cancel()`` can stop work that is
already running.  Keeping the complete production pipeline in a child process
gives the UI one reliable cancellation primitive: terminate that process.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import traceback
from collections.abc import Callable
from dataclasses import replace
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from ...recognition_queue import RecognitionWorkerLease

from ..pdf_rendering import render_high_resolution_page
from .service import (
    _resolve_grayscale,
    encode_agent3_page_png,
)
from .contracts import (
    Agent3Cancelled,
    Agent3Error,
    Agent3ProgressUpdate,
    ArrayLayoutPageInput,
    ElementRecognitionRequest,
    ElementRecognitionResult,
)


AGENT3_RENDER_MAX_SIDE = 6000
_POLL_SECONDS = 0.02
_worker_admission_guard: RecognitionWorkerLease | None = None


class Agent3ProcessRunner:
    """Run one production Agent3 job and make it synchronously terminable."""

    def __init__(self, process_target: Callable[..., None] | None = None) -> None:
        self._lock = threading.RLock()
        self._cancelled = threading.Event()
        self._process: multiprocessing.Process | None = None
        self._process_target = process_target or _agent3_process_main

    def reset(self) -> None:
        """Prepare for a new run after the previous process has been reaped."""

        with self._lock:
            if self._process is not None and self._process.is_alive():
                raise RuntimeError("Cannot reset Agent3 while its process is running.")
            self._cancelled.clear()

    def cancel(self) -> None:
        """Stop the active process without waiting for model or HTTP results."""

        self._cancelled.set()
        with self._lock:
            process = self._process
            if process is not None and process.is_alive():
                process.terminate()

    def run(
        self,
        request: ElementRecognitionRequest,
        pdf_path: str | Path,
        cached_images: dict[int, bytes],
        *,
        on_progress: Callable[[int, int], None] | None = None,
        on_step: Callable[[Agent3ProgressUpdate], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        admission: tuple[str, str] | None = None,
    ) -> tuple[ElementRecognitionResult, dict[int, bytes]]:
        """Run Agent3 in a spawned process and relay only progress/result data."""

        if self._is_cancelled(is_cancelled):
            raise Agent3Cancelled("Element recognition was cancelled.")

        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(
            target=_guarded_agent3_process,
            args=(self._process_target, request, str(pdf_path), cached_images, send, admission),
            name="agent3-model-process",
        )
        try:
            with self._lock:
                if self._cancelled.is_set():
                    receive.close()
                    send.close()
                    raise Agent3Cancelled("Element recognition was cancelled.")
                self._process = process
            # Spawning serializes cached page images and can take noticeable
            # time. Do it outside the lock so the GUI-side cancel call never
            # waits for process startup to finish.
            process.start()
            if self._cancelled.is_set():
                process.terminate()
        except BaseException:
            with self._lock:
                if self._process is process:
                    self._process = None
            receive.close()
            send.close()
            process.close()
            raise
        send.close()

        result: ElementRecognitionResult | None = None
        rendered_images: dict[int, bytes] = {}
        try:
            while True:
                if self._is_cancelled(is_cancelled):
                    self.cancel()
                    raise Agent3Cancelled("Element recognition was cancelled.")
                if receive.poll(_POLL_SECONDS):
                    try:
                        message = receive.recv()
                    except (EOFError, OSError):
                        message = None
                    if message is None:
                        if not process.is_alive():
                            break
                        continue
                    kind = message[0]
                    if kind == "progress":
                        if on_progress is not None:
                            on_progress(message[1], message[2])
                    elif kind == "step":
                        if on_step is not None:
                            on_step(message[1])
                    elif kind == "result":
                        result = message[1]
                        rendered_images = message[2]
                        break
                    elif kind == "error":
                        detail = f"{message[1]}: {message[2]}"
                        if len(message) > 3:
                            detail += "\n" + message[3]
                        raise Agent3Error(detail)
                elif not process.is_alive():
                    # Give the pipe a final chance to expose a message already
                    # flushed by the child just before it exited.
                    if receive.poll(0.05):
                        continue
                    break

            if self._is_cancelled(is_cancelled):
                raise Agent3Cancelled("Element recognition was cancelled.")
            if result is None:
                raise Agent3Error(
                    "Agent3 model process exited before returning a result "
                    f"(exit code {process.exitcode})."
                )
            return result, rendered_images
        finally:
            receive.close()
            self._reap(process)

    def _is_cancelled(
        self,
        callback: Callable[[], bool] | None,
    ) -> bool:
        return self._cancelled.is_set() or (
            callback is not None and callback()
        )

    def _reap(self, process: multiprocessing.Process) -> None:
        if process.is_alive():
            process.terminate()
        process.join(timeout=0.5)
        if process.is_alive():
            process.kill()
            process.join(timeout=0.5)
        with self._lock:
            if self._process is process:
                self._process = None
        process.close()


def _guarded_agent3_process(target, request, pdf_path, cached_images, connection, admission):
    """Watch the spawning parent and retain admission until actual process exit."""
    parent = multiprocessing.parent_process()
    if parent is None:
        raise RuntimeError("Agent3 requires a spawning parent")

    def stop_with_parent():
        parent.join()
        os._exit(1)  # Interrupt blocking HTTP/native work; OS releases the lease.

    threading.Thread(target=stop_with_parent, name="agent3-parent-watch", daemon=True).start()
    global _worker_admission_guard
    try:
        if admission is not None:
            # Retain this process-local reference through Python shutdown.
            # Only actual process termination releases its OS file lock.
            _worker_admission_guard = RecognitionWorkerLease(admission)
        target(request, pdf_path, cached_images, connection)
    except BaseException as exc:
        try:
            connection.send(("error", type(exc).__name__, str(exc), traceback.format_exc()))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


def _agent3_process_main(
    request: ElementRecognitionRequest,
    pdf_path: str,
    cached_images: dict[int, bytes],
    connection: Connection,
) -> None:
    """Child entry point containing every blocking Agent3 operation."""

    # Import here so ONNX, HTTP providers, and their native worker threads are
    # all created strictly inside the disposable process.
    from .service import Agent3ElementRecognizer

    send_lock = threading.Lock()

    def send(kind: str, *payload: Any) -> None:
        with send_lock:
            connection.send((kind, *payload))

    rendered_images: dict[int, bytes] = {}
    grayscale = _resolve_grayscale(None)

    def rendered_pages():
        for page in sorted(request.pages, key=lambda item: item.page_number):
            send(
                "step",
                Agent3ProgressUpdate(
                    "rendering",
                    "running",
                    page.page_number,
                    worker_name="agent3-render",
                ),
            )
            image = cached_images.get(page.page_number)
            detail = "cached" if image is not None else ""
            if image is None:
                image = render_high_resolution_page(
                    pdf_path,
                    page.page_number - 1,
                    width=AGENT3_RENDER_MAX_SIDE,
                    height=AGENT3_RENDER_MAX_SIDE,
                )
            # UI keeps the color original. Table OCR / Node crops use this page
            # (optional grayscale only). Line enhance is applied later only to
            # Tracker/Box YOLO detection.
            display_image = encode_agent3_page_png(
                image,
                max_side=AGENT3_RENDER_MAX_SIDE,
            )
            rendered_images[page.page_number] = display_image
            recognize_image = (
                encode_agent3_page_png(display_image, grayscale=True)
                if grayscale
                else display_image
            )
            send(
                "step",
                Agent3ProgressUpdate(
                    "rendering",
                    "completed",
                    page.page_number,
                    detail,
                    "agent3-render",
                ),
            )
            yield replace(page, png_bytes=recognize_image)

    try:
        recognizer = Agent3ElementRecognizer()
        result = recognizer.recognize_stream(
            request,
            rendered_pages(),
            on_progress=lambda completed, total: send(
                "progress", completed, total
            ),
            on_step=lambda update: send("step", update),
        )
        send("result", result, rendered_images)
    except BaseException as exc:  # noqa: BLE001 - serialize child failures
        try:
            send(
                "error",
                exc.__class__.__name__,
                str(exc),
                traceback.format_exc(),
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


