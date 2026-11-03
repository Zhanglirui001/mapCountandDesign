"""Review service used on the GUI thread, sharing its existing ViewModel."""

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import base64
import json
import os
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from ..application.ai_statistics.element_recognition.issues import ISSUE_DEFINITIONS
from ..application.ai_statistics.pdf_rendering import render_high_resolution_page
from ..application.ai_statistics.validation.service import validation_report_to_export_payload
from .corrections import correct_result, pixel_box
from .stage_review import StageReview
from .verification import issue_changes, result_checks, current_issues


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def validation_data(report):
    """Expose comparisons as well as the existing Count-import payload."""
    return {
        "passed": report.passed,
        "calculated_grand_total": report.calculated_grand_total,
        "ocr_grand_total": report.ocr_grand_total,
        "differences": [asdict(item) for item in report.differences],
        "inv_comparisons": [asdict(item) for item in report.inv_comparisons],
        "tracker_comparisons": [asdict(item) for item in report.tracker_comparisons],
        "count_import": validation_report_to_export_payload(report),
    }


class ReviewSession(StageReview):
    """Single owner: all methods are called by the bridge on the Qt thread."""

    def __init__(self, view_model, directory):
        self.vm = view_model
        self.directory = Path(directory)
        self.project_id = uuid4().hex
        self.revision = 0
        self._signature = None
        self._state_refs = ()
        self._receipts = {}
        self._history = []
        self._previews = {}
        self._evidence = {}
        self._source_fingerprint = None
        self._source_changed = False
        self._stage_reviews = {}

    @property
    def workflow(self):
        return self.vm.workflow

    def sync(self):
        snapshot = self.workflow.snapshot
        request = self.workflow.element_recognition_request_metadata
        fingerprint = None
        if snapshot.pdf_path is not None:
            try:
                stat = snapshot.pdf_path.stat()
                fingerprint = (str(snapshot.pdf_path), stat.st_size, stat.st_mtime_ns)
            except OSError:
                fingerprint = (str(snapshot.pdf_path), "unavailable")
        if snapshot.element_recognition_result is None or self._source_fingerprint is None:
            self._source_fingerprint = fingerprint
            self._source_changed = False
        elif fingerprint != self._source_fingerprint:
            self._source_changed = True
        # Progress notifications do not invalidate evidence. Human edits,
        # classification, metadata and model reruns do.
        signature = (snapshot.pdf_path, id(snapshot.page_classification),
                     id(snapshot.element_recognition_result), id(snapshot.overall_site_extractions),
                     repr(request), fingerprint)
        if signature != self._signature:
            old = self._signature
            invalid_from = (0 if old is None or signature[0:2] != old[0:2] or fingerprint != old[-1]
                            else 1 if signature[3] != old[3] else 2)
            self.revision += 1
            self._signature = signature
            self._state_refs = (snapshot.page_classification, snapshot.element_recognition_result,
                                snapshot.overall_site_extractions)
            self._previews.clear()
            self._stage_reviews = {k: v for k, v in self._stage_reviews.items() if int(k) < invalid_from}
        return snapshot

    def status(self):
        s = self.sync()
        return {
            "project_id": self.project_id, "revision": self.revision,
            "pdf_path": str(s.pdf_path) if s.pdf_path else None, "page_count": s.page_count,
            "running": self.vm.is_ai_running,
            "selected_stage": s.selected_stage.value,
            "stages": [v.value for v in s.stage_statuses],
            "error": s.error_message,
            "progress": {"classification": [s.classification_progress_completed, s.classification_progress_total],
                         "information": [s.information_progress_completed, s.information_progress_total],
                         "elements": [s.element_progress_completed, s.element_progress_total]},
            "has_result": s.element_recognition_result is not None,
            "source_pdf_changed": self._source_changed,
            "stage_reviews": dict(self._stage_reviews),
        }

    def context(self, detail="compact", stage=None):
        if detail not in ("compact", "full"):
            raise ValueError("detail must be compact or full")
        if stage is not None and (type(stage) is not int or stage not in (0, 1, 2)):
            raise ValueError("stage must be 0, 1, 2 or null")
        s = self.sync()
        request = self.workflow.element_recognition_request_metadata
        context = {
            **self.status(),
            "detail": detail,
            "available_evidence": [{"evidence_id": key, "page_number": item[1]}
                                   for key, item in self._evidence.items() if item[0] == self.revision],
            "goal": "Extract INV > Node > Rows, Tracker quantities/string counts and Box positions from electrical/PV drawings.",
            "coordinates": "1-based PDF page; normalized xyxy, top-left origin on the aspect-preserving rendered page after PDF rotation; no preview padding. Returned detection boxes are normalized too.",
            "rules": ["Use original drawing evidence; OCR totals can also be wrong.",
                      "Do not invent detections to force totals to match.",
                      "Only Nodes with changed geometry/count inputs are rebuilt; inspect their preview.",
                      "A cleared issue record is not proof of correctness; verify the targeted repair checks."],
            "tracker_definitions": [asdict(d) for d in request.tracker_definitions] if request else [],
            "expected_inv_names": list(request.expected_inv_names) if request else [],
            "panel_orientation": request.default_panel_orientation.value if request else None,
            "page_classification": [dict(page_number=p.page_number, page_type=p.page_type)
                                    for p in s.page_classification.pages] if s.page_classification else [],
            "supported_operations": ["initialize_page (missing layout output)", "add_detection (Node/Tracker/Box)", "update_detection (bbox/parent_id)",
                                     "delete_detection (Tracker/Box)", "delete_node (reparent children first)",
                                     "rename_node", "rename_inv (single-page INV)", "replace_table_rows"],
        }
        context["supported_operations"].extend(["mark_node_for_review (name, optional bbox, reason)",
                                                "resolve_node_review (entity_id, reason)"])
        if stage in (None, 0):
            context["agent1_review"] = self.classification_review(compact=detail == "compact")
        if stage in (None, 1):
            context["agent2_review"] = self.information_review()
        if stage in (None, 2):
            diagnostics = self.diagnostics()
            if detail == "compact":
                diagnostics.pop("raw", None)
            context["diagnostics"] = diagnostics
            context["issue_catalog"] = {
                code.value: {"severity": d.severity.value, "meaning": d.template}
                for code, d in ISSUE_DEFINITIONS.items()
            }
        return context

    def result(self):
        s = self.sync()
        if s.element_recognition_result is None:
            raise ValueError("No element recognition result is available")
        if self._source_changed:
            raise ValueError("Source PDF changed since recognition; reimport and rerun recognition")
        return s.element_recognition_result

    def require_idle(self):
        if self.vm.is_ai_running:
            raise ValueError("Wait for the active recognition task to finish")

    def issues(self, page_number=None, severity=None, offset=0, limit=50):
        s = self.sync()
        result = s.element_recognition_result
        if severity not in (None, "warning", "error"):
            raise ValueError("severity must be warning or error")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Use offset >= 0 and limit in 1..100")
        items = [asdict(i) for i in (current_issues(result) if result else ())
                 if (page_number is None or i.page_number == page_number)
                 and (severity is None or i.severity.value == severity)]
        for item in items:
            item["severity"] = item["severity"].value
            if item["bbox"] is not None:
                page = next((p for p in result.pages if p.page_number == item["page_number"]), None)
                item["bbox"] = self._normalized_box(item["bbox"], page) if page else None
        return {"project_id": self.project_id, "revision": self.revision,
                "total": len(items), "items": items[offset:offset + limit],
                "diagnostics": {k: v for k, v in self.diagnostics().items() if k != "raw"}}

    @staticmethod
    def _normalized_box(box, page):
        return [box["x1"] / page.image_width, box["y1"] / page.image_height,
                box["x2"] / page.image_width, box["y2"] / page.image_height]

    def evidence(self, page_number, bbox=None, max_size=1600):
        self.require_idle()
        s = self.sync()
        if type(page_number) is not int or not 1 <= page_number <= s.page_count:
            raise ValueError("Unknown PDF page")
        if self._source_changed:
            raise ValueError("Source PDF changed; reimport before reviewing")
        result = s.element_recognition_result
        page = next((p for p in result.pages if p.page_number == page_number), None) if result else None
        if page is None:
            return self.original_evidence(page_number, bbox, max_size)
        if type(max_size) is not int or not 256 <= max_size <= 2400:
            raise ValueError("max_size must be in 256..2400")
        s = self.workflow.snapshot
        evidence_id = uuid4().hex
        # Match recognition image dimensions; grayscale/enhancement do not alter geometry.
        data = render_high_resolution_page(s.pdf_path, page_number - 1,
                                           width=page.image_width, height=page.image_height)
        original = Image.open(BytesIO(data)).convert("RGB")
        if original.size != (page.image_width, page.image_height):
            # PDFium rounds raster dimensions, and Agent3 may subsequently
            # thumbnail a 6001px edge to 6000px. Correct that rounding only.
            if (abs(original.width - page.image_width) > 2
                    or abs(original.height - page.image_height) > 2):
                raise ValueError("Rendered dimensions differ from recognition; rerun recognition")
            original = original.resize((page.image_width, page.image_height), Image.Resampling.LANCZOS)
        crop = pixel_box(bbox, page).to_tuple() if bbox is not None else (0, 0, *original.size)
        display_scale = max(1.0, max(crop[2] - crop[0], crop[3] - crop[1]) / max_size)
        font = ImageFont.load_default(size=round(13 * display_scale))
        annotated = original.copy()
        draw = ImageDraw.Draw(annotated)
        for d in page.detections:
            color = {"Node": "red", "Tracker": "blue", "Box": "green"}.get(d.kind, "orange")
            draw.rectangle(d.bbox.to_tuple(), outline=color, width=round(2 * display_scale))
            draw.text((d.bbox.x1, d.bbox.y1), d.entity_id, fill=color, font=font)
        assets = self.directory / self.project_id / "evidence" / evidence_id
        images = []
        for name, img in (("original", original), ("overlay", annotated)):
            img = img.crop(crop)
            img.thumbnail((max_size, max_size))
            stream = BytesIO()
            img.save(stream, format="PNG")
            assets.mkdir(parents=True, exist_ok=True)
            path = assets / f"{name}.png"
            path.write_bytes(stream.getvalue())
            images.append({"type": "image", "data": base64.b64encode(stream.getvalue()).decode(),
                           "mimeType": "image/png", "path": str(path.resolve())})
        payload = result.to_dict()
        page_data = next(p for p in payload["pages"] if p["page_number"] == page_number)
        for d in page_data["detections"]:
            d["bbox"] = self._normalized_box(d["bbox"], page)
        for issue in page_data["issues"]:
            if issue["bbox"] is not None:
                issue["bbox"] = self._normalized_box(issue["bbox"], page)
        # Other page geometry remains explicitly pixel-space in the full snapshot.
        metadata = {"evidence_id": evidence_id, "project_id": self.project_id,
                    "revision": self.revision, "page": page_data,
                    "nodes": [{"entity_id": n.recognition_id, "name": n.name,
                               "ocr_string_count": n.ocr_string_count,
                               "has_ocr_string_count": n.has_ocr_string_count,
                               "tracker_counts": [asdict(q) for q in n.tracker_counts],
                               "box_position": n.box_position,
                               "rows": [{"name": r.name, "rackings": list(r.rackings),
                                         "entries": [{"rackings": e.rackings, "length_str": e.length_str}
                                                     for e in r.racking_entries]} for r in n.rows]}
                              for inv in result.reviewed_tree.invs for n in inv.nodes
                              if n.source.page_number == page_number],
                    "bbox": bbox or [0, 0, 1, 1],
                    "coordinates": "detections and issues: normalized xyxy; table_bbox/table_bboxes: recognition pixels",
                    "source_pdf": str(s.pdf_path),
                    "source_image_sha256": sha256(data).hexdigest(),
                    "result_path": str((assets / "result.json").resolve())}
        write_json(assets / "result.json", payload)
        write_json(assets / "evidence.json", metadata)
        self._evidence[evidence_id] = (self.revision, page_number)
        return {"metadata": metadata, "images": images}

    def _check(self, project_id, base_revision):
        self.require_idle()
        self.sync()
        if project_id != self.project_id:
            raise ValueError("Project mismatch")
        if type(base_revision) is not int or base_revision != self.revision:
            raise ValueError(f"Revision conflict: current revision is {self.revision}; fetch fresh evidence")

    def preview(self, project_id, base_revision, operations, evidence_ids):
        self._check(project_id, base_revision)
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise ValueError("Supply evidence_ids from inspected images")
        pages = {op.get("page_number") for op in operations if isinstance(op, dict)} if isinstance(operations, list) else set()
        covered = set()
        for key in evidence_ids:
            evidence = self._evidence.get(key)
            if evidence is None or evidence[0] != self.revision:
                raise ValueError("Evidence missing or stale; fetch fresh evidence")
            covered.add(evidence[1])
        if not pages <= covered:
            raise ValueError("Every edited page requires current evidence")
        before = self.correction_base()
        request = self.workflow.element_recognition_request_metadata
        if request is None:
            raise ValueError("Recognition metadata is unavailable")
        for op in operations:
            if op.get("op") == "initialize_page":
                data = render_high_resolution_page(self.workflow.snapshot.pdf_path, op["page_number"] - 1,
                                                   width=3200, height=3200)
                if Image.open(BytesIO(data)).size != (op.get("image_width"), op.get("image_height")):
                    raise ValueError("Initialize dimensions must match original evidence")
        candidate = correct_result(before, request, operations)
        expected_nodes = {n.recognition_id for inv in before.reviewed_tree.invs for n in inv.nodes}
        expected_nodes -= {op["entity_id"] for op in operations if op["op"] == "delete_node"}
        actual_nodes = {n.recognition_id for inv in candidate.reviewed_tree.invs for n in inv.nodes}
        expected_pages = {p.page_number for p in before.pages}
        expected_pages.update(op["page_number"] for op in operations if op["op"] == "initialize_page")
        if not expected_nodes <= actual_nodes or expected_pages != {p.page_number for p in candidate.pages}:
            raise ValueError("Correction preview would lose unrelated pages or Nodes")
        report = self.workflow.validate_element_result(candidate)
        token = uuid4().hex
        self._previews[token] = (self.revision, candidate, operations, evidence_ids)
        old_nodes = {n.recognition_id: n for inv in before.reviewed_tree.invs for n in inv.nodes}
        return {"preview_id": token, "project_id": self.project_id, "revision": self.revision,
                "changed_nodes": [n.recognition_id for inv in candidate.reviewed_tree.invs for n in inv.nodes
                                  if n != old_nodes.get(n.recognition_id)],
                "issues_before": len(before.all_issues), "issues_after": len(candidate.all_issues),
                "issues": candidate.to_dict()["issues"] + [i for p in candidate.to_dict()["pages"] for i in p["issues"]],
                "validation_before": validation_data(self.workflow.validate_element_result(before)),
                "validation": validation_data(report),
                "issue_changes": issue_changes(before, candidate),
                "repair_checks": result_checks(candidate, report, request),
                "warning": "Only Nodes with changed geometry/count inputs are rebuilt. Counts and unresolved errors require follow-up even if issue totals fall."}

    def apply(self, project_id, base_revision, preview_id, request_id):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("request_id must contain 1..128 characters")
        fingerprint = (project_id, base_revision, preview_id)
        previous = self._receipts.get(request_id)
        if previous:
            if previous[0] != fingerprint:
                raise ValueError("request_id was already used for another correction")
            return previous[1]
        self._check(project_id, base_revision)
        preview = self._previews.get(preview_id)
        if preview is None or preview[0] != self.revision:
            raise ValueError("Preview missing or stale")
        if len(preview) > 4 and preview[1] == "stage":
            return self.apply_stage_preview(preview, project_id, base_revision, preview_id, request_id)
        _, candidate, operations, evidence_ids = preview
        before = self.correction_base()
        # Persist business-result before/after snapshots before publishing.
        record = {"schema_version": "codex-review-v1", "request_id": request_id,
                  "timestamp": datetime.now(timezone.utc).isoformat(),
                  "project_id": project_id, "base_revision": base_revision,
                  "source_pdf": str(self.workflow.snapshot.pdf_path),
                  "diagnostics_before": self.diagnostics(),
                  "operations": operations, "evidence_ids": evidence_ids,
                  "before": before.to_dict(), "after": candidate.to_dict()}
        path = self.directory / self.project_id / "corrections" / f"{uuid4().hex}.json"
        write_json(path, {**record, "status": "prepared"})
        try:
            if self.workflow.snapshot.element_recognition_result is None:
                self.vm.recover_element_result(candidate)
            else:
                self.vm.replace_element_result(candidate)
        except Exception:
            write_json(path, {**record, "status": "failed"})
            raise
        self.sync()
        actual = self.result()
        actual_report = self.workflow.validate_element_result(actual)
        verification = result_checks(actual, actual_report, self.workflow.element_recognition_request_metadata)
        receipt = {"project_id": project_id, "revision": self.revision,
                   "request_id": request_id, "audit_path": str(path.resolve()),
                   "remaining_issues": len(actual.all_issues),
                   "preview_matches_published": actual.reviewed_tree == candidate.reviewed_tree and actual.pages == candidate.pages,
                   "repair_checks": verification,
                   "issue_changes": issue_changes(before, actual)}
        self._receipts[request_id] = (fingerprint, receipt)
        self._history.append({"receipt": receipt, "operations": operations})
        # The prepared record remains recoverable even if receipt persistence fails.
        write_json(path, {**record, "after": actual.to_dict(), "status": "applied", "receipt": receipt})
        return receipt

    def summary(self):
        self.require_idle()
        status = self.status()
        status["review_complete"] = {"0", "1", "2"} <= self._stage_reviews.keys()
        if not status["has_result"]:
            return {**status, "corrections": self._history}
        result = self.result()
        report = self.workflow.validate_element_result(result)
        checks = result_checks(result, report, self.workflow.element_recognition_request_metadata)
        manual_path = self.directory / self.project_id / "manual-review.json"
        write_json(manual_path, {"project_id": self.project_id, "revision": self.revision,
                   "source_pdf": str(self.workflow.snapshot.pdf_path),
                   "missing_node_candidates": checks["missing_node_candidates"],
                   "manual_review_queue": checks["manual_review_queue"],
                   "scope": "Candidates only; not counted as devices. A Node absent from both table and detections may remain undiscovered."})
        path = self.directory / self.project_id / "reviewed-result.json"
        write_json(path, {"project_id": self.project_id, "revision": self.revision,
                          "result": result.to_dict(),
                          "stage_reviews": self._stage_reviews,
                          "review_complete": status["review_complete"],
                          "repair_checks": checks,
                          "validation": validation_data(report)})
        return {**status, "remaining_issues": len(current_issues(result)),
                "manual_review_path": str(manual_path.resolve()),
                "repair_complete": checks["status"] == "checks_passed",
                "repair_checks": checks,
                "corrections": self._history, "result_path": str(path.resolve()),
                "warnings": (["Panel orientation is unknown; polarity/offset interpretation needs review"]
                             if self.workflow.element_recognition_request_metadata.default_panel_orientation.value == "unknown" else []),
                "validation": validation_data(report)}
