from __future__ import annotations

import threading
import time
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import pytest

from count_and_design.application.recognition_queue import RecognitionTicket

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


def _heartbeat_child(_request, pdf_path, _images, connection):
    directory = Path(pdf_path)
    (directory / "worker.json").write_text(json.dumps({"pid": os.getpid()}))
    connection.send(("step", Agent3ProgressUpdate("tracker_detection", "running", 1)))
    for tick in range(600):
        (directory / "heartbeat").write_text(str(tick))
        time.sleep(.1)


def _recognition_owner(directory, connection):
    ticket = RecognitionTicket(Path(directory) / "queue")
    assert ticket.poll()[0]
    Agent3ProcessRunner(process_target=_heartbeat_child).run(
        ElementRecognitionRequest(pages=(), tracker_definitions=()), directory, {},
        admission=ticket.worker_admission,
        on_step=lambda update: connection.send("ready"))


def _pid_running(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().split(")", 1)[1].split()[0] == "Z":
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_owner_crash_stops_model_and_never_admits_overlapping_worker(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    read, write = ctx.Pipe(duplex=False)
    owner = ctx.Process(target=_recognition_owner, args=(str(tmp_path), write))
    follower = None
    pid = None
    try:
        owner.start()
        write.close()
        assert read.poll(15), "Agent3 worker did not start"
        assert read.recv() == "ready"
        pid = json.loads((tmp_path / "worker.json").read_text())["pid"]
        follower = RecognitionTicket(tmp_path / "queue")
        assert follower.poll() == (False, 1)
        owner.terminate()
        owner.join(5)
        assert not owner.is_alive()
        deadline = time.monotonic() + 5
        while True:
            admitted, _ = follower.poll()
            if admitted:
                assert not _pid_running(pid), "Queue admitted work while orphaned model was alive"
                break
            assert time.monotonic() < deadline, "Orphaned worker retained admission indefinitely"
            time.sleep(.02)
    finally:
        if owner.is_alive():
            owner.terminate()
            owner.join(5)
        if pid is not None and _pid_running(pid):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                import signal
                os.kill(pid, signal.SIGKILL)
        if follower is not None:
            follower.close()
        read.close()
        owner.close()


def test_revoked_ticket_prevents_late_spawn_from_starting_model(tmp_path):
    ticket = RecognitionTicket(tmp_path / "queue")
    admission = ticket.worker_admission
    ticket.close()
    follower = RecognitionTicket(tmp_path / "queue")
    try:
        assert follower.poll()[0]
        runner = Agent3ProcessRunner(process_target=_heartbeat_child)
        with pytest.raises(Agent3Error, match="admission was revoked"):
            runner.run(ElementRecognitionRequest(pages=(), tracker_definitions=()),
                       str(tmp_path), {}, admission=admission)
        assert not (tmp_path / "worker.json").exists()
    finally:
        follower.close()


def test_worker_lease_survives_release_of_gui_ticket(tmp_path):
    owner = RecognitionTicket(tmp_path / "queue")
    runner = Agent3ProcessRunner(process_target=_heartbeat_child)
    started = threading.Event()
    errors = []
    follower = None
    def execute():
        try:
            runner.run(ElementRecognitionRequest(pages=(), tracker_definitions=()),
                       str(tmp_path), {}, admission=owner.worker_admission,
                       on_step=lambda update: started.set())
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=execute)
    try:
        thread.start()
        assert started.wait(15), errors
        owner.close()
        follower = RecognitionTicket(tmp_path / "queue")
        assert follower.poll() == (False, 1)
        runner.cancel()
        thread.join(3)
        assert not thread.is_alive()
        assert errors and isinstance(errors[0], Agent3Cancelled)
        assert follower.poll() == (True, 0)
    finally:
        runner.cancel()
        thread.join(5)
        owner.close()
        if follower is not None:
            follower.close()
