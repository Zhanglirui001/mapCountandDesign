"""Authenticated loopback transport; queued requests run only on Qt's thread."""

from concurrent.futures import Future, TimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from queue import Empty, Queue
import secrets
from threading import Thread
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer

from ..application.ai_statistics.models import AIStatisticsStageStatus
from .review_session import ReviewSession, write_json
from .runtime import window_id


class GuiBridge(QObject):
    def __init__(self, window, discovery_file):
        super().__init__(window)
        self.window = window
        self.tab = window._ai_statistics_tab
        self.vm = self.tab._view_model
        self.tab._tool_session_active = True
        self.discovery_file = Path(discovery_file).resolve()
        self.window_id = window_id(self.discovery_file)
        self.session = ReviewSession(self.vm, self.discovery_file.parent / "projects")
        self.queue = Queue(maxsize=32)
        self.jobs = {}
        self.active_job = None
        self._dispatching = False
        self.token = secrets.token_urlsafe(32)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(100)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                # Local clients must possess the discovery-file token. Reject
                # browser origins rather than opening a cross-origin write API.
                if (self.path != "/tools" or self.headers.get("Origin") is not None
                        or not secrets.compare_digest(self.headers.get("Authorization", ""),
                                                      "Bearer " + bridge.token)):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2 * 1024 * 1024:
                        raise ValueError("Invalid request length")
                    self.connection.settimeout(10)
                    request = json.loads(self.rfile.read(length))
                    if not isinstance(request, dict):
                        raise ValueError("Request must be an object")
                    future = Future()
                    bridge.queue.put_nowait((request, future))
                    try:
                        response = future.result(timeout=90)
                    except TimeoutError:
                        # A pending command is cancelled; running commands may
                        # finish. apply_corrections uses request_id for retries.
                        future.cancel()
                        response = {"error": "GUI request timed out; query state before retrying writes"}
                except Exception as exc:
                    response = {"error": str(exc)}
                payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        Thread(target=self.server.serve_forever, daemon=True).start()
        write_json(self.discovery_file, {"window_id": self.window_id, "pid": os.getpid(), "url": f"http://127.0.0.1:{self.server.server_port}/tools",
                                         "token": self.token})

    def close(self):
        self.tab._tool_session_active = False
        self.timer.stop()
        self.server.shutdown()
        self.server.server_close()
        while not self.queue.empty():
            _, future = self.queue.get_nowait()
            future.cancel()
        if self.discovery_file.exists():
            try:
                data = json.loads(self.discovery_file.read_text(encoding="utf-8"))
                if data.get("token") == self.token:
                    self.discovery_file.unlink()
            except (OSError, ValueError):
                pass

    def _tick(self):
        if self._dispatching:
            return
        self._dispatching = True
        try:
            self._process_tick()
        finally:
            self._dispatching = False

    def _process_tick(self):
        try:
            self._advance_job()
        except Exception as exc:
            if self.active_job:
                self.jobs[self.active_job].update(status="failed", error=str(exc))
                self.active_job = None
        # One command per tick avoids starving worker callbacks and GUI events.
        try:
            request, future = self.queue.get_nowait()
        except Empty:
            return
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result({"result": self.dispatch(request["tool"], request.get("arguments", {}))})
        except Exception as exc:
            future.set_result({"error": str(exc)})

    def _advance_job(self):
        if not self.active_job:
            return
        job = self.jobs[self.active_job]
        if self.vm.is_ai_running:
            job.update(status="queued" if self.vm.state.recognition_queued else "running",
                       queue_position=self.vm.state.queue_position)
            return
        if self.vm.recognition_cancelled:
            job.update(status="cancelled", queue_position=0)
            self.active_job = None
            return
        stage = job["stage"]
        status = self.vm.workflow.snapshot.stage_statuses[stage]
        if status == AIStatisticsStageStatus.FAILED:
            job.update(status="failed", error=self.vm.workflow.snapshot.error_message)
            self.active_job = None
            return
        if status not in {AIStatisticsStageStatus.RESULT_READY, AIStatisticsStageStatus.CONFIRMED,
                          AIStatisticsStageStatus.NEEDS_CONFIRM}:
            job.update(status="cancelled")
            self.active_job = None
            return
        if stage == 1 and job.get("panel_orientation"):
            self.vm.update_overall_site_review(self.vm.workflow.snapshot.overall_site_extractions,
                                              job["panel_orientation"])
        self.session.sync()
        job.update(status="completed", review_required=True,
                   next_action="get_project_context, inspect evidence, correct if needed, confirm_stage_review")
        self.active_job = None

    def dispatch(self, tool, args):
        methods = {
            "get_project_context": self.session.context,
            "list_review_issues": self.session.issues,
            "get_review_evidence": self.session.evidence,
            "preview_corrections": self.session.preview,
            "apply_corrections": self.session.apply,
            "get_review_summary": self.session.summary,
            "preview_stage_corrections": self.session.preview_stage_corrections,
            "confirm_stage_review": self.session.confirm_stage_review,
        }
        if tool == "get_job_status":
            job_id = args.get("job_id")
            if job_id is not None and job_id not in self.jobs:
                raise ValueError("Unknown job_id")
            self._advance_job()
            return {**self._status(), "job": self.jobs.get(job_id or self.active_job)}
        if tool == "start_session":
            self.window.showNormal()
            self.window.raise_()
            self.window.activateWindow()
            return self._status()
        if tool == "cancel_recognition":
            job_id = self.active_job
            self.vm.cancel_ai_model()
            self._advance_job()
            return {**self._status(), "job": self.jobs.get(job_id)}
        if tool in {"import_pdf", "start_recognition", "preview_corrections", "apply_corrections",
                    "preview_stage_corrections", "confirm_stage_review"}:
            if self.active_job or self.tab._upload_in_progress:
                raise ValueError("Wait for the active upload/recognition job")
        if tool == "import_pdf":
            # An imported but unprocessed PDF is already user work. Protect it
            # at the GUI boundary too, including callers bypassing the MCP tool.
            if self.vm.workflow.snapshot.pdf_path and args.get("replace_existing") is not True:
                raise ValueError(
                    "This window already contains a PDF, even if recognition has not started. "
                    "Use start_session(new_window=True) to import another project. "
                    "Use replace_existing=True only when the user explicitly requests replacement."
                )
            self.tab.load_pdf_for_tools(args["path"])
            # Even reimporting the same filename represents a new document run.
            self.session = ReviewSession(self.vm, self.discovery_file.parent / "projects")
            return self._status()
        if tool == "start_recognition":
            self.session.require_idle()
            snapshot = self.vm.workflow.snapshot
            stage = args.get("stage", "all")
            orientation = args.get("panel_orientation")
            if orientation is not None and orientation not in {"P-N", "P-S", "P-E", "P-W", "P-B", "N-B"}:
                raise ValueError("Invalid panel_orientation")
            if stage == "all":
                if snapshot.page_classification is None:
                    index = 0
                elif not snapshot.overall_site_extractions:
                    index = 1
                else:
                    index = 2
            elif type(stage) is int and stage in (0, 1, 2):
                index = stage
            else:
                raise ValueError("stage must be all, 0, 1 or 2")
            # Codex must explicitly inspect and acknowledge each preceding stage.
            self.session.sync()
            for prior in range(index):
                if str(prior) not in self.session._stage_reviews:
                    raise ValueError(f"Agent{prior + 1} requires confirm_stage_review before continuing")
                if self.vm.workflow.snapshot.stage_statuses[prior] != AIStatisticsStageStatus.CONFIRMED:
                    raise ValueError("Previous workflow stage is not confirmed")
            if self.vm.workflow.snapshot.selected_stage.index != index:
                self.vm.set_current_stage(index)
            if index == 2 and orientation:
                self.vm.update_overall_site_review(snapshot.overall_site_extractions, orientation)
            if not self.vm.start_ai_model():
                raise ValueError("Recognition did not start")
            job_id = uuid4().hex
            self.jobs[job_id] = {"job_id": job_id, "stage": index,
                                 "status": "queued" if self.vm.state.recognition_queued else "running",
                                 "queue_position": self.vm.state.queue_position}
            self.jobs[job_id]["panel_orientation"] = orientation
            self.active_job = job_id
            return {**self._status(), "job_id": job_id, "job": self.jobs[job_id]}
        if tool in methods:
            result = methods[tool](**args)
            return {**result, "window_id": self.window_id} if isinstance(result, dict) else result
        raise ValueError("Unknown tool")

    def _status(self):
        return {**self.session.status(), "window_id": self.window_id,
                "recognition_queued": self.vm.state.recognition_queued,
                "queue_position": self.vm.state.queue_position}
