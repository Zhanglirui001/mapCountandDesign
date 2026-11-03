"""Agent3 admission is FIFO across windows; Agent1/2 remain independent."""

import os
from pathlib import Path
import subprocess
import sys
import sqlite3
import time
from threading import Event
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from count_and_design.application.recognition_queue import RecognitionTicket
from count_and_design.application.ai_statistics import AIStatisticsWorkflow
from count_and_design.application.ai_statistics.models import PageClassification, PageClassificationResult
from count_and_design.gui.viewmodels.ai_statistics import AIStatisticsViewModel


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNT_DESIGN_QUEUE_DIR", str(tmp_path / "queue"))


def wait_for(condition):
    for _ in range(600):
        if condition():
            return
        QTest.qWait(10)
    pytest.fail("Recognition queue did not advance")


def test_fifo_cancellation_and_no_duplicate_admission():
    first, second, third = RecognitionTicket(), RecognitionTicket(), RecognitionTicket()
    try:
        assert first.poll() == (True, 0)
        assert second.poll() == (False, 1)
        assert third.poll() == (False, 2)
        second.close()
        assert third.poll() == (False, 1)
        first.close()
        assert third.poll() == (True, 0)
    finally:
        first.close()
        second.close()
        third.close()


def test_killed_process_cannot_leave_queue_blocked():
    code = ("from count_and_design.application.recognition_queue import RecognitionTicket; "
            "t=RecognitionTicket(); print(t.poll(), flush=True); input()")
    child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ticket = None
    try:
        assert child.stdout.readline().strip() == "(True, 0)"
        ticket = RecognitionTicket()
        assert ticket.poll() == (False, 1)
        child.terminate()
        child.wait(timeout=15)
        assert ticket.poll() == (True, 0)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=15)
        if ticket is not None:
            ticket.close()


def make_vm(tmp_path, name, entered, release, *, fail=False):
    def classify(*args, **kwargs):
        entered.set()
        assert release.wait(10), "test worker was not released"
        if fail:
            raise RuntimeError("controlled provider failure")
        return PageClassificationResult(pages=(PageClassification(1, "Array Layout"),))
    vm = AIStatisticsViewModel(AIStatisticsWorkflow(SimpleNamespace(classify=classify)))
    vm.load_pdf(tmp_path / (name + ".pdf"), 1)
    return vm


@pytest.mark.parametrize("finish", ["success", "failure", "cancel"])
def test_two_windows_run_concurrently(tmp_path, app, finish):
    entered_a, entered_b, release_a, release_b = Event(), Event(), Event(), Event()
    a = make_vm(tmp_path, "A", entered_a, release_a, fail=finish == "failure")
    b = make_vm(tmp_path, "B", entered_b, release_b)
    try:
        assert a.start_ai_model()
        wait_for(entered_a.is_set)
        assert b.start_ai_model()
        wait_for(entered_b.is_set)
        assert a.is_ai_running and b.is_ai_running
        assert not a.state.recognition_queued and not b.state.recognition_queued
        assert a._recognition_ticket.directory != b._recognition_ticket.directory
        assert not a.start_ai_model() and not b.start_ai_model()
        with pytest.raises(RuntimeError):
            b.load_pdf(tmp_path / "replacement.pdf", 1)
        if finish == "cancel":
            a.cancel_ai_model()
            QTest.qWait(350)
            assert a.is_ai_running  # Its own slot stays held until worker exit.
            assert b.is_ai_running and not b.recognition_cancelled
        release_a.set()
        wait_for(lambda: not a.is_ai_running)
        assert b.is_ai_running
        release_b.set()
        wait_for(lambda: not b.is_ai_running)
        assert b.workflow.snapshot.pdf_path.name == "B.pdf"
        assert b.workflow.snapshot.page_classification is not None
    finally:
        release_a.set()
        release_b.set()
        assert a.shutdown()
        assert b.shutdown()


def test_cancel_and_close_queued_window_without_starting_worker(tmp_path, app):
    entered, release = Event(), Event()
    vm = make_vm(tmp_path, "waiting", entered, release)
    blocker = RecognitionTicket(vm._recognition_directory)
    try:
        assert vm.start_ai_model()
        vm.cancel_ai_model()
        assert vm.recognition_cancelled
        assert not vm.is_ai_running and not vm.state.recognition_queued
        assert vm.state.run_enabled
        assert vm.start_ai_model()
        assert vm.shutdown()
        assert not entered.is_set()
        assert not vm.is_ai_running
    finally:
        release.set()
        vm.shutdown()
        blocker.close()


@pytest.mark.parametrize("action", ["resume", "cancel"])
def test_busy_database_retains_retry_and_allows_cancellation(tmp_path, app, action):
    entered, release = Event(), Event()
    vm = make_vm(tmp_path, "busy-db", entered, release)
    blocker = RecognitionTicket(vm._recognition_directory)
    errors = []
    vm.operation_failed.connect(errors.append)
    db = None
    try:
        assert vm.start_ai_model()
        db = sqlite3.connect(vm._recognition_directory / "queue.sqlite3")
        db.execute("BEGIN EXCLUSIVE")
        QTest.qWait(800)  # Exercise real QTimer retries while SQLite is locked.
        assert vm.state.recognition_queued
        assert vm._queue_timer.isActive()
        assert not errors and not entered.is_set()
        blocker.close()  # Completion also remains possible while DB is locked.
        if action == "cancel":
            started = time.monotonic()
            vm.cancel_ai_model()
            assert time.monotonic() - started < .5
            assert not vm.is_ai_running
        db.rollback()
        db.close()
        db = None
        if action == "resume":
            wait_for(entered.is_set)
            release.set()
            wait_for(lambda: not vm.is_ai_running)
        else:
            QTest.qWait(350)
            assert not entered.is_set()
        following = RecognitionTicket()
        try:
            assert following.poll() == (True, 0)
        finally:
            following.close()
    finally:
        if db is not None:
            db.rollback()
            db.close()
        release.set()
        vm.shutdown()
        blocker.close()


def test_queued_navigation_browses_results_without_changing_task_stage(tmp_path, app):
    entered, release = Event(), Event()
    vm = make_vm(tmp_path, "browse", entered, release)
    blocker = RecognitionTicket(vm._recognition_directory)
    try:
        vm.workflow.complete_page_classification(
            PageClassificationResult(pages=(PageClassification(1, "Array Layout"),)))
        vm.confirm_current_stage()
        vm.set_current_stage(0)
        assert vm.start_ai_model()
        token = vm._recognition_ticket.token
        vm.set_current_stage(0)  # Clicking the current stage is harmless.
        vm.set_current_stage(1)  # Existing stages remain available for browsing.
        assert vm.state.current_stage == 1 and vm.state.recognition_queued
        assert vm._recognition_ticket.token == token
        assert not entered.is_set()
        blocker.close()
        wait_for(entered.is_set)
        assert vm.state.current_stage == 0  # Run the originally submitted task.
        release.set()
        wait_for(lambda: not vm.is_ai_running)
    finally:
        release.set()
        vm.shutdown()
        blocker.close()


def prepared_vm(tmp_path, name, stage, entered, release):
    from conftest import sample_agent2_extraction
    vm = make_vm(tmp_path, name, entered, release)
    if stage == 0:
        return vm

    def recognize(*args, **kwargs):
        entered.set()
        assert release.wait(10), "test worker was not released"
        raise RuntimeError("controlled recognition failure")

    vm.workflow._overall_site_recognizer = SimpleNamespace(recognize_pages=recognize)
    vm.workflow._element_recognizer = SimpleNamespace(recognize=recognize)
    vm.workflow._element_process_runner = None
    vm.load_pdf(tmp_path / (name + ".pdf"), 2)
    vm.workflow.complete_page_classification(PageClassificationResult(
        pages=(PageClassification(1, "Array Layout"), PageClassification(2, "Overall Layout")),
        page_images={1: b"cached image", 2: b"cached image"},
    ))
    vm.confirm_current_stage()
    if stage == 2:
        vm.workflow.complete_overall_site_recognition((sample_agent2_extraction(),))
        vm.confirm_current_stage()
    return vm



@pytest.mark.parametrize("stage_a,stage_b", [(1, 0), (2, 0), (2, 1), (0, 2), (1, 2)])
def test_agent_stages_overlap_across_windows(tmp_path, app, stage_a, stage_b):
    entered_a, entered_b, release_a, release_b = Event(), Event(), Event(), Event()
    a = prepared_vm(tmp_path, "A", stage_a, entered_a, release_a)
    b = prepared_vm(tmp_path, "B", stage_b, entered_b, release_b)
    try:
        assert a.start_ai_model()
        wait_for(entered_a.is_set)
        assert b.start_ai_model()
        wait_for(entered_b.is_set)
        assert a.is_ai_running and b.is_ai_running
        assert not a.state.recognition_queued and not b.state.recognition_queued
        assert a.state.queue_position == b.state.queue_position == 0
        a.cancel_ai_model()
        release_a.set()
        wait_for(lambda: not a.is_ai_running)
        assert b.is_ai_running and not b.recognition_cancelled
    finally:
        release_a.set()
        release_b.set()
        assert a.shutdown()
        assert b.shutdown()


def test_synchronous_recognition_does_not_wait_for_other_window(tmp_path, app):
    entered, release = Event(), Event()
    a = make_vm(tmp_path, "A", entered, release)
    released = Event()
    released.set()
    b = make_vm(tmp_path, "B", Event(), released)
    try:
        assert a.start_ai_model()
        wait_for(entered.is_set)
        assert b.run_ai_model().total_pages == 1
        assert a.is_ai_running
    finally:
        release.set()
        assert a.shutdown()
        assert b.shutdown()


@pytest.mark.parametrize("finish", ["success", "failure", "cancel"])
def test_agent3_fifo_waits_for_worker_exit_and_keeps_other_agents_free(tmp_path, app, finish):
    from count_and_design.application.ai_statistics import ElementRecognitionResult, ReviewedTree

    entered = [Event() for _ in range(4)]
    release = [Event() for _ in range(4)]
    windows = [prepared_vm(tmp_path, str(i), 2 if i < 3 else 0, entered[i], release[i])
               for i in range(4)]
    a, b, c, classifier = windows

    def recognize(*args, **kwargs):
        entered[0].set()
        assert release[0].wait(10)
        if finish == "failure":
            raise RuntimeError("controlled recognition failure")
        return ElementRecognitionResult(reviewed_tree=ReviewedTree())

    a.workflow._element_recognizer.recognize = recognize
    try:
        assert a.start_ai_model()
        wait_for(entered[0].is_set)
        assert b.start_ai_model() and c.start_ai_model()
        assert b.state.recognition_queued and b.state.queue_position == 1
        assert c.state.recognition_queued and c.state.queue_position == 2
        assert not entered[1].is_set() and not entered[2].is_set()
        assert a._recognition_ticket.directory == b._recognition_ticket.directory
        assert classifier.start_ai_model()
        wait_for(entered[3].is_set)
        assert not classifier.state.recognition_queued
        assert not b.start_ai_model()
        with pytest.raises(RuntimeError):
            b.load_pdf(tmp_path / "replacement.pdf", 1)
        if finish == "cancel":
            a.cancel_ai_model()
            QTest.qWait(350)
            assert not entered[1].is_set(), "Cancellation released a still-running worker"
        release[0].set()
        wait_for(entered[1].is_set)
        assert not a.is_ai_running
        assert c.state.recognition_queued and not entered[2].is_set()
        release[1].set()
        wait_for(entered[2].is_set)
        assert not b.is_ai_running
        assert not classifier.recognition_cancelled
    finally:
        for event in release:
            event.set()
        for vm in windows:
            assert vm.shutdown()


@pytest.mark.parametrize("action", ["cancel", "close"])
def test_removing_queued_agent3_preserves_fifo_for_following_window(tmp_path, app, action):
    entered = [Event() for _ in range(3)]
    release = [Event() for _ in range(3)]
    windows = [prepared_vm(tmp_path, str(i), 2, entered[i], release[i]) for i in range(3)]
    a, b, c = windows
    try:
        assert a.start_ai_model()
        wait_for(entered[0].is_set)
        assert b.start_ai_model() and c.start_ai_model()
        if action == "cancel":
            b.cancel_ai_model()
        else:
            assert b.shutdown()
        assert not b.is_ai_running and not entered[1].is_set()
        wait_for(lambda: c.state.queue_position == 1)
        assert not entered[2].is_set()
        release[0].set()
        wait_for(entered[2].is_set)
        assert not entered[1].is_set()
    finally:
        for event in release:
            event.set()
        for vm in windows:
            assert vm.shutdown()


def test_separate_gui_processes_run_concurrently(tmp_path):
    # Both processes use the same user queue root, as packaged GUI windows do.
    code = """
import sys
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from count_and_design.application.ai_statistics import AIStatisticsWorkflow
from count_and_design.application.ai_statistics.models import PageClassification, PageClassificationResult
from count_and_design.gui.viewmodels.ai_statistics import AIStatisticsViewModel
app = QApplication([])
entered, release = Event(), Event()
def classify(*args, **kwargs):
    entered.set()
    assert release.wait(20)
    return PageClassificationResult(pages=(PageClassification(1, 'Array Layout'),))
vm = AIStatisticsViewModel(AIStatisticsWorkflow(SimpleNamespace(classify=classify)))
vm.load_pdf(Path(sys.argv[1]) / 'drawing.pdf', 1)
try:
    assert vm.start_ai_model()
    deadline = time.monotonic() + 8
    while not entered.is_set():
        assert time.monotonic() < deadline, 'Other window blocked recognition'
        QTest.qWait(10)
    print('running', flush=True)
    input()
finally:
    release.set()
    assert vm.shutdown()
"""
    processes = []
    try:
        for _ in range(2):
            child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            processes.append(child)
            assert child.stdout.readline().strip() == "running"
        assert all(child.poll() is None for child in processes)
        first_out, first_err = processes[0].communicate("done\n", timeout=15)
        assert processes[0].returncode == 0, first_err
        assert processes[1].poll() is None
        second_out, second_err = processes[1].communicate("done\n", timeout=15)
        assert processes[1].returncode == 0, second_err
    finally:
        for child in processes:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=15)


def test_agent3_queues_across_independent_gui_processes(tmp_path):
    code = """
import sys
import time
from pathlib import Path
from threading import Event
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
sys.path.insert(0, str(Path.cwd() / 'tests'))
from test_recognition_queue import prepared_vm
app = QApplication([])
entered, release = Event(), Event()
vm = prepared_vm(Path(sys.argv[1]), sys.argv[2], 2, entered, release)
try:
    assert vm.start_ai_model()
    if sys.argv[2] == 'first':
        deadline = time.monotonic() + 8
        while not entered.is_set():
            assert time.monotonic() < deadline
            QTest.qWait(10)
        print('running', flush=True)
        input()
    else:
        assert vm.state.recognition_queued and vm.state.queue_position == 1
        assert not entered.is_set()
        print('queued', flush=True)
        input()
        deadline = time.monotonic() + 8
        while not entered.is_set():
            assert time.monotonic() < deadline, 'Agent3 queue did not advance'
            QTest.qWait(10)
        assert not vm.state.recognition_queued
        print('advanced', flush=True)
finally:
    release.set()
    assert vm.shutdown()
"""
    processes = []
    try:
        for name, expected in [("first", "running"), ("second", "queued")]:
            child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path), name],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            processes.append(child)
            assert child.stdout.readline().strip() == expected
        _, err = processes[0].communicate("finish\n", timeout=15)
        assert processes[0].returncode == 0, err
        out, err = processes[1].communicate("advance\n", timeout=15)
        assert processes[1].returncode == 0, err
        assert "advanced" in out
    finally:
        for child in processes:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=15)
