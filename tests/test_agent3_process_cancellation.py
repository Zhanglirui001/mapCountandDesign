from __future__ import annotations

import threading
import time
import pytest

from count_and_design.application.ai_statistics import (
    Agent3Cancelled,
    Agent3ProgressUpdate,
    ElementRecognitionRequest,
)
from count_and_design.application.ai_statistics.element_recognition.process_runner import (
    Agent3ProcessRunner,
)
from count_and_design.application.ai_statistics.element_recognition.contracts import Agent3Error


def _blocking_agent3_child(_request, _pdf_path, _cached_images, connection) -> None:
    connection.send(
        ("step", Agent3ProgressUpdate("tracker_detection", "running", 1))
    )
    time.sleep(30)


def _failing_agent3_child(_request, _pdf_path, _cached_images, connection) -> None:
    connection.send(("error", "ValueError", "bad detector output", "Traceback: failing_detector.py:42"))
    connection.close()


def test_agent3_process_error_preserves_type_and_traceback():
    runner = Agent3ProcessRunner(process_target=_failing_agent3_child)
    runner.reset()
    with pytest.raises(Agent3Error, match="ValueError: bad detector output") as caught:
        runner.run(ElementRecognitionRequest(pages=(), tracker_definitions=()), "unused.pdf", {})
    assert "failing_detector.py:42" in str(caught.value)


def test_agent3_process_cancel_terminates_without_waiting_for_model_result() -> None:
    runner = Agent3ProcessRunner(process_target=_blocking_agent3_child)
    runner.reset()
    started = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            runner.run(
                ElementRecognitionRequest(pages=(), tracker_definitions=()),
                "unused.pdf",
                {},
                on_step=lambda _update: started.set(),
            )
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(10), errors

    cancelled_at = time.monotonic()
    runner.cancel()
    worker.join(2)

    assert not worker.is_alive()
    assert time.monotonic() - cancelled_at < 2
    assert errors and isinstance(errors[0], Agent3Cancelled)
