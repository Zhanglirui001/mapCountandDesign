"""Evidence-backed review gates for classification and overall-site OCR."""

from dataclasses import asdict
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4
from hashlib import sha256
import base64

from PIL import Image

from ..application.ai_statistics.pdf_rendering import render_high_resolution_page
from ..application.ai_statistics.info_recognition.overall_site import (
    OverallSiteExtraction, TrackerOcrFields, TrackerFieldValue, NumStrDetail,
    InvOcrRow, InvStringCount,
)
from ..application.ai_statistics.element_recognition.agent2_adapter import Agent2ElementContract
from .corrections import pixel_box


class StageReview:
    def correction_base(self):
        s = self.sync()
        if self._source_changed:
            raise ValueError("Source PDF changed; reimport")
        if s.element_recognition_result is not None:
            return self.result()
        if not {"0", "1"} <= self._stage_reviews.keys():
            raise ValueError("Review Agent1 and Agent2 before recovering Agent3")
        from ..application.ai_statistics.models import ReviewedTree
        from ..application.ai_statistics.element_recognition.contracts import ElementRecognitionResult
        return ElementRecognitionResult(ReviewedTree())

    def classification_review(self, margin=0.05, *, compact=False):
        s = self.workflow.snapshot
        pages = []
        for p in s.page_classification.pages if s.page_classification else ():
            raw = p.source.raw or {}
            scores = raw.get("category_scores", [])
            score = max((v.get("score", -1) for v in scores), default=p.source.confidence)
            threshold = raw.get("min_score")
            pages.append({"page_number": p.page_number, "page_type": p.page_type,
                          "score": score, "threshold": threshold,
                          "category_scores": sorted(scores, key=lambda v: v.get("score", -1), reverse=True)[:3] if compact else scores,
                          "near_threshold": threshold is not None and score is not None
                          and abs(score - threshold) <= margin})
        overall = [p["page_number"] for p in pages if p["page_type"] == "Overall Layout"]
        layout = [p["page_number"] for p in pages if p["page_type"] == "Array Layout"]
        gaps = [n for n in range(min(layout), max(layout) + 1) if n not in layout] if layout else []
        neighbors = {n + d for n in overall + layout for d in (-1, 1)
                     if 1 <= n + d <= s.page_count}
        suspicious = [{"page_number": p["page_number"],
                       "reasons": (["near_threshold"] if p["near_threshold"] else []) +
                                  (["layout_gap"] if p["page_number"] in gaps else [])}
                      for p in pages if p["near_threshold"] or p["page_number"] in gaps]
        return {"overall_pages": overall, "layout_pages": layout, "pages": pages,
                "category_scores_truncated": compact,
                "layout_gaps": gaps, "boundary_neighbors": sorted(neighbors - set(layout)),
                "near_threshold_margin": margin,
                "suspicious_pages": suspicious,
                "review_pages": sorted(p["page_number"] for p in suspicious),
                "review_policy": "Summarize existing results and list suspicious pages with reasons first. Images are optional for triage; fetch only when needed to resolve a specific doubt or make an edit. Do not inspect every page or automatically expand to neighbors.",
                "rule": "Scores are embedding similarities, not calibrated probabilities. Layout continuity is a clue, never a forced classification. review_pages are candidates, not a mandatory image checklist."}

    def information_review(self):
        rows = self.workflow.snapshot.overall_site_extractions
        from ..application.ai_statistics.info_recognition import build_agent4_input
        findings = []
        for r in rows:
            if not r.tracker_ocr_fields:
                findings.append({"page_number": r.page_number, "code": "missing_tracker_fields"})
            if not r.inv_list or not r.inv_rows:
                findings.append({"page_number": r.page_number, "code": "incomplete_inv_structure"})
            if r.inv_count != len(set(r.inv_list)) or len(r.inv_list) != len(set(r.inv_list)):
                findings.append({"page_number": r.page_number, "code": "inv_count_or_duplicate_mismatch"})
            if set(r.inv_list) != {v.identifier for v in r.inv_rows}:
                findings.append({"page_number": r.page_number, "code": "inv_list_row_mismatch"})
        return {"extractions": [asdict(r) for r in rows], "findings": findings,
                "derived_totals": build_agent4_input(rows) if rows else {},
                "tracker_definitions": [asdict(t) for t in self.workflow.agent2_tracker_definitions],
                "checks": ["Triage the existing extracted fields, warnings and totals first; list concrete missing or inconsistent items.",
                           "Fetch original crops only for a specific doubt or correction; do not reread all overall tables by default.",
                           "INV rows are the available Agent2 structure; do not invent Node hierarchy absent from the drawing."]}

    def diagnostics(self):
        s = self.workflow.snapshot
        result = s.element_recognition_result
        expected = [p.page_number for p in s.page_classification.pages
                    if p.page_type == "Array Layout"] if s.page_classification else []
        returned = [p.page_number for p in result.pages] if result else []
        return {"error": s.error_message, "stage_statuses": [v.value for v in s.stage_statuses],
                "last_step": s.element_progress_current_step,
                "last_page_number": s.element_progress_page_number,
                "last_detail": s.element_progress_detail,
                "active_tasks": [asdict(t) for t in s.element_progress_active_tasks],
                "last_failure": s.element_failure_context,
                "expected_layout_pages": expected, "returned_pages": returned,
                "missing_result_pages": sorted(set(expected) - set(returned)),
                "execution_issues": [{**asdict(i), "severity": i.severity.value} for i in result.all_issues
                                     if i.severity.value == "error"] if result else [],
                "raw": result.raw if result else {},
                "failure_localization": "Use last_step/page/detail and active tasks as failure context. With parallel tasks these are candidates, not proof of the failing page. Original evidence is available even without output."}

    def original_evidence(self, page_number, bbox, max_size):
        from .review_session import write_json
        if type(max_size) is not int or not 256 <= max_size <= 2400:
            raise ValueError("max_size must be in 256..2400")
        data = render_high_resolution_page(self.workflow.snapshot.pdf_path, page_number - 1,
                                           width=3200, height=3200)
        img = Image.open(BytesIO(data)).convert("RGB")
        size = SimpleNamespace(image_width=img.width, image_height=img.height)
        if bbox is not None:
            img = img.crop(pixel_box(bbox, size).to_tuple())
        img.thumbnail((max_size, max_size))
        stream = BytesIO()
        img.save(stream, "PNG")
        evidence_id = uuid4().hex
        directory = self.directory / self.project_id / "evidence" / evidence_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "original.png"
        path.write_bytes(stream.getvalue())
        metadata = {"evidence_id": evidence_id, "project_id": self.project_id,
                    "revision": self.revision, "page_number": page_number,
                    "bbox": bbox or [0, 0, 1, 1], "source_pdf": str(self.workflow.snapshot.pdf_path),
                    "source_image_sha256": sha256(data).hexdigest(),
                    "has_element_output": False,
                    "image_width": size.image_width, "image_height": size.image_height,
                    "classification": next((p for p in self.classification_review(compact=True)["pages"]
                                            if p["page_number"] == page_number), None),
                    "agent2": [asdict(r) for r in self.workflow.snapshot.overall_site_extractions
                               if page_number in self._extraction_pages(r)]}
        write_json(directory / "evidence.json", metadata)
        self._evidence[evidence_id] = (self.revision, page_number)
        return {"metadata": metadata, "images": [{"type": "image", "mimeType": "image/png",
                "path": str(path.resolve()), "data": base64.b64encode(stream.getvalue()).decode()}]}

    def _covered(self, evidence_ids):
        covered = set()
        for key in evidence_ids:
            item = self._evidence.get(key)
            if item is None or item[0] != self.revision:
                raise ValueError("Evidence missing or stale; fetch fresh evidence")
            covered.add(item[1])
        return covered

    @staticmethod
    def _extraction_pages(row):
        return {row.page_number, *row.raw.get("consolidated_page_numbers", ())}

    def preview_stage_corrections(self, project_id, base_revision, stage, changes, evidence_ids, reason):
        self._check(project_id, base_revision)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Supply an evidence-grounded reason")
        covered = self._covered(evidence_ids)
        if type(stage) is not int or stage not in (0, 1) or not isinstance(changes, list) or not changes:
            raise ValueError("stage must be 0/1 with nonempty changes")
        s = self.workflow.snapshot
        if stage == 0:
            known = {p.page_number for p in s.page_classification.pages} if s.page_classification else set()
            for c in changes:
                if set(c) != {"page_number", "page_type"} or type(c["page_number"]) is not int or c["page_number"] not in known or c["page_type"] not in {"Overall Layout", "Array Layout", "Other"}:
                    raise ValueError("Classification changes require valid page_number and page_type")
            candidate = changes
        else:
            # Accept the same full extraction schema returned by information_review.
            candidate = tuple(self._extraction(c) for c in changes)
            if len({r.page_number for r in candidate}) != len(candidate):
                raise ValueError("Duplicate extraction pages")
            overall = set(self.classification_review()["overall_pages"])
            groups = [self._extraction_pages(r) for r in candidate]
            represented = set().union(*groups)
            if represented != overall or sum(map(len, groups)) != len(represented):
                raise ValueError("Supply complete extractions for every Overall Site page")
            Agent2ElementContract.from_overall_site(candidate)
        numbers = {c["page_number"] for c in changes} if stage == 0 else represented
        if not numbers <= covered:
            raise ValueError("Every edited page requires current evidence")
        token = uuid4().hex
        self._previews[token] = (self.revision, "stage", stage, candidate, changes, evidence_ids, reason)
        return {"preview_id": token, "stage": stage, "changes": changes,
                "invalidates": [1, 2, 3] if stage == 0 else [2, 3],
                "tracker_definitions": [asdict(t) for t in Agent2ElementContract.from_overall_site(candidate).tracker_definitions] if stage == 1 else []}

    @staticmethod
    def _extraction(c):
        if type(c.get("page_number")) is not int or type(c.get("inv_count", 0)) is not int or c.get("inv_count", 0) < 0:
            raise ValueError("Invalid extraction page/count")
        fields = []
        for t in c.get("tracker_ocr_fields", []):
            fields.append(TrackerOcrFields(**{k: TrackerFieldValue(**t[k]) for k in
                          ("module_model", "module_stc_dc_rating_w", "string_count", "num_str")},
                          num_str_details=tuple(NumStrDetail(**d) for d in t.get("num_str_details", []))))
        return OverallSiteExtraction(**{**c, "tracker_ocr_fields": tuple(fields),
            "inv_list": tuple(c.get("inv_list", [])), "warnings": tuple(c.get("warnings", [])),
            "inv_rows": tuple(InvOcrRow(r["identifier"], tuple(InvStringCount(**v) for v in r.get("string_counts", [])))
                              for r in c.get("inv_rows", []))})

    def apply_stage_preview(self, preview, project_id, base_revision, preview_id, request_id):
        from .review_session import write_json
        _, _, stage, candidate, changes, evidence_ids, reason = preview
        before = self.classification_review() if stage == 0 else self.information_review()
        path = self.directory / project_id / "corrections" / f"{uuid4().hex}.json"
        record = {"stage": stage, "before": before, "changes": changes,
                  "reason": reason, "evidence_ids": evidence_ids, "request_id": request_id}
        write_json(path, {**record, "status": "prepared"})
        if stage == 0:
            for c in candidate:
                self.vm.update_page_classification(c["page_number"], c["page_type"])
        else:
            orientation = self.workflow.snapshot.overall_site_orientation
            self.vm.replace_overall_site_extractions(candidate)
            if orientation:
                self.vm.update_overall_site_review(candidate, orientation)
        self.sync()
        receipt = {"project_id": project_id, "revision": self.revision,
                   "request_id": request_id, "audit_path": str(path.resolve())}
        if stage == 0:
            pages = self.classification_review(compact=True)["pages"]
            receipt["applied_changes"] = changes
            receipt["classification_counts"] = {
                kind: sum(p["page_type"] == kind for p in pages)
                for kind in ("Overall Layout", "Array Layout", "Other")
            }
        self._receipts[request_id] = ((project_id, base_revision, preview_id), receipt)
        self._history.append({"receipt": receipt, "stage": stage, "changes": changes})
        after = self.classification_review() if stage == 0 else self.information_review()
        write_json(path, {**record, "after": after, "status": "applied", "receipt": receipt})
        return receipt

    def confirm_stage_review(self, project_id, base_revision, stage, evidence_ids, notes):
        self._check(project_id, base_revision)
        if type(stage) is not int or stage not in (0, 1, 2) or not isinstance(notes, str) or not notes.strip():
            raise ValueError("Supply stage 0/1/2 and review notes")
        required = (set(self.classification_review()["review_pages"]) if stage == 0 else
                    set(self.classification_review()["overall_pages"]) if stage == 1 else
                    set(self.diagnostics()["expected_layout_pages"]))
        # Triage can use model output alone. Only actual edits require images.
        inspected = self._covered(evidence_ids)
        if stage == 1:
            Agent2ElementContract.from_overall_site(self.workflow.snapshot.overall_site_extractions)
        if stage == 2:
            result = self.result()
            if self.diagnostics()["missing_result_pages"]:
                raise ValueError("Cannot confirm while layout pages lack output")
            if any(not any(d.kind == "Node" for d in p.detections) for p in result.pages
                   if p.page_number in required):
                raise ValueError("Cannot confirm empty layout pages; recover Nodes or correct classification")
        from ..application.ai_statistics.models import AIStatisticsStageStatus
        if self.workflow.snapshot.stage_statuses[stage] not in {AIStatisticsStageStatus.RESULT_READY,
                AIStatisticsStageStatus.NEEDS_CONFIRM, AIStatisticsStageStatus.CONFIRMED}:
            raise ValueError("Stage is not ready for review")
        self.vm.set_current_stage(stage)
        if self.workflow.snapshot.stage_statuses[stage] != AIStatisticsStageStatus.CONFIRMED:
            self.vm.confirm_current_stage()
        self._stage_reviews[str(stage)] = {"revision": self.revision, "notes": notes,
                                          "evidence_ids": evidence_ids,
                                          "review_mode": "result_triage",
                                          "inspected_pages": sorted(inspected)}
        from .review_session import write_json
        write_json(self.directory / self.project_id / "stage-reviews.json", self._stage_reviews)
        return self.status()
