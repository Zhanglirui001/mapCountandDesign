"""Build an aggregate ``INV_fake`` from ordered project quantity targets."""

from __future__ import annotations

import copy
import json
import math
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from ...models import (
    DataFillTarget,
    DataFillTerm,
    Inv,
    Node,
    Project,
    QuantityRef,
)
from .formula import (
    DataFillFormulaError,
    format_data_fill_formula,
    format_quantity_ref,
    validate_formula_references,
)


FAKE_INV_NAME = "INV_fake"
_TARGET_LIMIT = 2_000_000_000


class DataFillError(ValueError):
    """A validation or solver error with a stable machine-readable code."""

    def __init__(self, code: str, **params: object) -> None:
        self.code = code
        self.params = params
        super().__init__(code)


@dataclass(frozen=True)
class DataFillConstraints:
    """Ordered soft equality targets selected by the user."""

    targets: tuple[DataFillTarget, ...] = ()

    def validate(self) -> None:
        if not self.targets or not any(item.enabled for item in self.targets):
            raise DataFillError("no_constraints")
        labels: set[str] = set()
        for item in self.targets:
            label = item.label.strip()
            if not label:
                raise DataFillError("target_empty_name")
            if label in labels:
                raise DataFillError("target_duplicate_name", name=label)
            labels.add(label)
            if not item.terms:
                raise DataFillError("formula_empty", name=label)
            if not -_TARGET_LIMIT <= item.target <= _TARGET_LIMIT:
                raise DataFillError("target_out_of_range", name=label)


@dataclass(frozen=True)
class QuantitySnapshot:
    node_count: int = 0
    string_count: int = 0
    rackings: dict[str, int] = field(default_factory=dict)
    products: dict[str, int] = field(default_factory=dict)


@dataclass
class NodeTemplate:
    template_id: str
    node: Node
    sample_count: int
    contribution: QuantitySnapshot
    sample_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DataFillPrepared:
    current: QuantitySnapshot
    templates: tuple[NodeTemplate, ...]
    racking_names: tuple[str, ...]
    product_names: tuple[str, ...]
    targets: tuple[DataFillTarget, ...]


@dataclass(frozen=True)
class ConstraintResult:
    target_id: str
    label: str
    formula: str
    target: int
    current: int
    actual: int

    @property
    def absolute_deviation(self) -> int:
        return abs(self.actual - self.target)

    @property
    def relative_deviation(self) -> float:
        return self.absolute_deviation / max(abs(self.target), 1)


@dataclass(frozen=True)
class TemplateResult:
    template_id: str
    sample_names: tuple[str, ...]
    sample_count: int
    sample_ratio: float
    added_count: int


@dataclass(frozen=True)
class DataFillResult:
    constraints: DataFillConstraints
    current: QuantitySnapshot
    actual: QuantitySnapshot
    constraint_results: tuple[ConstraintResult, ...]
    template_results: tuple[TemplateResult, ...]
    templates: tuple[NodeTemplate, ...] = field(repr=False)
    exact: bool = False
    timed_out: bool = False
    used_fallback: bool = False

    @property
    def added_node_count(self) -> int:
        return sum(item.added_count for item in self.template_results)


@dataclass(frozen=True)
class _CompiledTarget:
    target: DataFillTarget
    current: int
    coefficients: tuple[int, ...]


class DataFillService:
    """Prepare, solve and materialize project data-fill requests."""

    def __init__(self, max_time_seconds: float = 10.0) -> None:
        self.max_time_seconds = max(0.1, max_time_seconds)
        self._active_solver: object | None = None
        self._solver_lock = threading.Lock()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._solver_lock:
            solver = self._active_solver
        if solver is not None:
            stop = getattr(solver, "StopSearch", None)
            if stop is not None:
                stop()

    def prepare(self, project: Project) -> DataFillPrepared:
        real_invs = [inv for inv in project.invs if inv.name != FAKE_INV_NAME]
        if not real_invs:
            raise DataFillError("no_real_invs")

        grouped: dict[str, NodeTemplate] = {}
        ordered_signatures: list[str] = []
        for inv in real_invs:
            for node in inv.nodes:
                effective = self._materialize_effective_node(project, inv, node)
                signature = self._node_signature(effective)
                if signature not in grouped:
                    template = NodeTemplate(
                        template_id=f"T{len(ordered_signatures) + 1}",
                        node=effective,
                        sample_count=0,
                        contribution=self._count_nodes(project, [effective]),
                    )
                    grouped[signature] = template
                    ordered_signatures.append(signature)
                grouped[signature].sample_count += 1
                grouped[signature].sample_names.append(f"{inv.name}/{node.name}")

        if not grouped:
            raise DataFillError("no_sample_nodes")

        templates = tuple(grouped[key] for key in ordered_signatures)
        current = self._sum_template_counts(templates)
        racking_names = tuple(dict.fromkeys(
            [item.name for item in project.rackings_list] + sorted(current.rackings)
        ))
        product_names = tuple(dict.fromkeys(
            [item.name for item in project.product_presets] + sorted(current.products)
        ))
        targets = self._reconcile_targets(
            project.data_fill_targets, current, racking_names, product_names
        )
        return DataFillPrepared(current, templates, racking_names, product_names, targets)

    def solve(
        self,
        project: Project,
        constraints: DataFillConstraints,
        progress_callback: Callable[[float], None] | None = None,
    ) -> DataFillResult:
        constraints.validate()
        prepared = self.prepare(project)
        compiled = self._compile_targets(prepared, constraints)
        self._cancelled.clear()

        last_progress_time = 0.0
        last_progress_display: float | None = None

        def report_progress(counts: list[int], *, force: bool = False) -> None:
            nonlocal last_progress_time, last_progress_display
            if progress_callback is None:
                return
            similarity = self._similarity_for_counts(compiled, counts)
            display_value = round(similarity, 1)
            now = time.monotonic()
            if display_value == last_progress_display:
                return
            if not force and now - last_progress_time < 0.1:
                return
            last_progress_time = now
            last_progress_display = display_value
            progress_callback(similarity)

        report_progress([0] * len(prepared.templates), force=True)
        try:
            counts, timed_out = self._solve_cp_sat(
                prepared, constraints, progress_callback=report_progress
            )
            used_fallback = False
        except (ImportError, ModuleNotFoundError):
            counts = self._fallback_solution(
                prepared, constraints, progress_callback=report_progress
            )
            timed_out = False
            used_fallback = True
        except DataFillError as exc:
            if exc.code == "cancelled":
                raise
            counts = self._fallback_solution(
                prepared, constraints, progress_callback=report_progress
            )
            timed_out = exc.code == "solver_no_result"
            used_fallback = True
        if self._cancelled.is_set():
            raise DataFillError("cancelled")
        report_progress(counts, force=True)
        return self._build_result(
            prepared, constraints, counts,
            timed_out=timed_out, used_fallback=used_fallback,
        )

    def build_fake_inv(self, result: DataFillResult) -> Inv | None:
        if result.added_node_count <= 0:
            return None
        fake = Inv(name=FAKE_INV_NAME)
        next_index = 1
        count_by_id = {
            item.template_id: item.added_count for item in result.template_results
        }
        for template in result.templates:
            for _ in range(count_by_id.get(template.template_id, 0)):
                node = template.node.clone()
                node.name = f"Node_fake_{next_index}"
                fake.nodes.append(node)
                next_index += 1
        return fake

    @staticmethod
    def _materialize_effective_node(project: Project, inv: Inv, node: Node) -> Node:
        effective = node.clone()
        if effective.racking_pitch is None:
            effective.racking_pitch = copy.deepcopy(inv.racking_pitch or project.racking_pitch)
        if effective.cluster_wire is None:
            effective.cluster_wire = copy.deepcopy(inv.cluster_wire or project.cluster_wire)
        if effective.lynx_preset is None:
            effective.lynx_preset = inv.lynx_preset or project.lynx_preset
        return effective

    @staticmethod
    def _node_signature(node: Node) -> str:
        data = node.to_dict()
        data.pop("name", None)
        rows = data.get("rows", [])
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    row.pop("name", None)
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _count_nodes(project: Project, nodes: list[Node]) -> QuantitySnapshot:
        rackings: Counter[str] = Counter()
        products: Counter[str] = Counter()
        string_count = 0
        for node in nodes:
            for row in node.rows:
                for entry in row.racking_entries:
                    if not entry.rackings:
                        continue
                    rackings[entry.rackings] += 1
                    racking = project.get_rackings(entry.rackings)
                    if racking is None:
                        continue
                    for product_name in racking.product_presets:
                        products[product_name] += 1
                        preset = project.get_product_preset(product_name)
                        if preset is not None:
                            string_count += preset.branch_count
        return QuantitySnapshot(
            node_count=len(nodes),
            string_count=string_count,
            rackings=dict(rackings),
            products=dict(products),
        )

    @staticmethod
    def _sum_template_counts(templates: tuple[NodeTemplate, ...]) -> QuantitySnapshot:
        rackings: Counter[str] = Counter()
        products: Counter[str] = Counter()
        nodes = 0
        strings = 0
        for template in templates:
            count = template.sample_count
            nodes += count
            strings += count * template.contribution.string_count
            rackings.update({k: count * v for k, v in template.contribution.rackings.items()})
            products.update({k: count * v for k, v in template.contribution.products.items()})
        return QuantitySnapshot(nodes, strings, dict(rackings), dict(products))

    @staticmethod
    def quantity_value(snapshot: QuantitySnapshot, ref: QuantityRef) -> int:
        if ref.kind == "nodes":
            return snapshot.node_count
        if ref.kind == "strings":
            return snapshot.string_count
        if ref.kind == "racking":
            return snapshot.rackings.get(ref.name, 0)
        return snapshot.products.get(ref.name, 0)

    @classmethod
    def target_value(cls, snapshot: QuantitySnapshot, target: DataFillTarget) -> int:
        return sum(
            term.coefficient * cls.quantity_value(snapshot, term.ref)
            for term in target.terms
        )

    @staticmethod
    def _builtin_id(ref: QuantityRef) -> str:
        return f"builtin:{ref.kind}:{ref.name}"

    @staticmethod
    def _builtin_label(ref: QuantityRef) -> str:
        return format_quantity_ref(ref)

    @classmethod
    def _new_builtin(cls, ref: QuantityRef, current: QuantitySnapshot) -> DataFillTarget:
        return DataFillTarget(
            target_id=cls._builtin_id(ref),
            label=cls._builtin_label(ref),
            terms=[DataFillTerm(ref, 1)],
            target=cls.quantity_value(current, ref),
            enabled=False,
            builtin_ref=ref,
        )

    @classmethod
    def _reconcile_targets(
        cls,
        saved_targets: list[DataFillTarget],
        current: QuantitySnapshot,
        racking_names: tuple[str, ...],
        product_names: tuple[str, ...],
    ) -> tuple[DataFillTarget, ...]:
        catalog = [
            QuantityRef("nodes"),
            QuantityRef("strings"),
            *(QuantityRef("racking", name) for name in racking_names),
            *(QuantityRef("product", name) for name in product_names),
        ]
        catalog_set = set(catalog)
        result: list[DataFillTarget] = []
        present: set[QuantityRef] = set()
        for original in copy.deepcopy(saved_targets):
            if original.is_builtin:
                ref = original.builtin_ref
                if ref is None or ref not in catalog_set or ref in present:
                    continue
                original.target_id = cls._builtin_id(ref)
                original.label = cls._builtin_label(ref)
                original.terms = [DataFillTerm(ref, 1)]
                present.add(ref)
            else:
                if not original.target_id:
                    original.target_id = f"custom:{uuid.uuid4()}"
            result.append(original)

        for ref in catalog:
            if ref in present:
                continue
            target = cls._new_builtin(ref, current)
            same_kind = [
                index for index, item in enumerate(result)
                if item.builtin_ref is not None and item.builtin_ref.kind == ref.kind
            ]
            if same_kind:
                insert_at = same_kind[-1] + 1
            else:
                insert_at = next(
                    (index for index, item in enumerate(result) if not item.is_builtin),
                    len(result),
                )
            result.insert(insert_at, target)
            present.add(ref)
        return tuple(result)

    def _compile_targets(
        self, prepared: DataFillPrepared, constraints: DataFillConstraints
    ) -> list[_CompiledTarget]:
        rackings = set(prepared.racking_names)
        products = set(prepared.product_names)
        compiled: list[_CompiledTarget] = []
        for target in constraints.targets:
            try:
                validate_formula_references(target.terms, rackings, products)
            except DataFillFormulaError as exc:
                raise DataFillError(exc.code, token=exc.token, name=target.label) from exc
            if not target.enabled:
                continue
            coefficients = tuple(
                self.target_value(template.contribution, target)
                for template in prepared.templates
            )
            compiled.append(_CompiledTarget(
                target=target,
                current=self.target_value(prepared.current, target),
                coefficients=coefficients,
            ))
        return compiled

    def _derive_max_added(
        self, prepared: DataFillPrepared, constraints: DataFillConstraints
    ) -> int:
        estimates: list[float] = [0.0]
        sample_total = prepared.current.node_count
        for item in self._compile_targets(prepared, constraints):
            delta = item.target.target - item.current
            if delta == 0:
                continue
            directional = [
                coefficient if delta > 0 else -coefficient
                for coefficient in item.coefficients
                if (coefficient > 0) == (delta > 0) and coefficient != 0
            ]
            if directional:
                estimates.append(abs(delta) / min(directional))
            sample_contribution = sum(
                template.sample_count * coefficient
                for template, coefficient in zip(prepared.templates, item.coefficients)
            )
            if sample_contribution and (sample_contribution > 0) == (delta > 0):
                estimates.append(abs(delta) * sample_total / abs(sample_contribution))
        estimate = max(estimates)
        return max(32, math.ceil(estimate * 2 + sample_total))

    def _solve_cp_sat(
        self,
        prepared: DataFillPrepared,
        constraints: DataFillConstraints,
        *,
        progress_callback: Callable[[list[int]], None] | None = None,
    ) -> tuple[list[int], bool]:
        from ortools.sat.python import cp_model

        max_added = self._derive_max_added(prepared, constraints)
        model = cp_model.CpModel()
        variables = [
            model.NewIntVar(0, max_added, f"x_{index}")
            for index in range(len(prepared.templates))
        ]

        class _SolutionProgress(cp_model.CpSolverSolutionCallback):
            def on_solution_callback(callback_self) -> None:
                if progress_callback is None:
                    return
                progress_callback([
                    int(callback_self.Value(variable)) for variable in variables
                ])

        total_added = sum(variables)
        objectives: list[tuple[object, str]] = []

        for index, item in enumerate(self._compile_targets(prepared, constraints)):
            actual = item.current + sum(
                coefficient * variable
                for coefficient, variable in zip(item.coefficients, variables)
            )
            lower = item.current + sum(min(0, coefficient * max_added) for coefficient in item.coefficients)
            upper = item.current + sum(max(0, coefficient * max_added) for coefficient in item.coefficients)
            dev_upper = max(abs(lower - item.target.target), abs(upper - item.target.target))
            deviation = model.NewIntVar(0, max(1, dev_upper), f"target_dev_{index}")
            model.AddAbsEquality(deviation, actual - item.target.target)
            objectives.append((deviation, f"target_{index}"))

        sample_total = prepared.current.node_count
        ratio_errors = []
        for index, (template, variable) in enumerate(zip(prepared.templates, variables)):
            ratio_dev = model.NewIntVar(
                0, max(1, 2 * sample_total * max_added * len(variables)),
                f"ratio_dev_{index}",
            )
            model.AddAbsEquality(
                ratio_dev,
                sample_total * variable - template.sample_count * total_added,
            )
            ratio_errors.append(ratio_dev)
        if ratio_errors:
            objectives.append((sum(ratio_errors), "ratio"))
        objectives.append((total_added, "total"))
        objectives.append((sum((i + 1) * x for i, x in enumerate(variables)), "stable"))

        deadline = time.monotonic() + self.max_time_seconds
        best: list[int] | None = None
        timed_out = False
        for objective_index, (objective, _name) in enumerate(objectives):
            if self._cancelled.is_set():
                raise DataFillError("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            model.Minimize(objective)
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = max(
                0.05, remaining / (len(objectives) - objective_index)
            )
            solver.parameters.num_search_workers = 1
            solver.parameters.random_seed = 0
            with self._solver_lock:
                self._active_solver = solver
            try:
                status = solver.Solve(model, _SolutionProgress())
            finally:
                with self._solver_lock:
                    self._active_solver = None
            if self._cancelled.is_set():
                raise DataFillError("cancelled")
            if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                if best is None:
                    raise DataFillError("solver_no_result")
                timed_out = True
                break
            best = [int(solver.Value(variable)) for variable in variables]
            value = int(solver.Value(objective))
            model.Add(objective == value)
            if status != cp_model.OPTIMAL:
                timed_out = True

        if best is None:
            raise DataFillError("solver_no_result")
        return best, timed_out

    def _fallback_solution(
        self,
        prepared: DataFillPrepared,
        constraints: DataFillConstraints,
        *,
        progress_callback: Callable[[list[int]], None] | None = None,
    ) -> list[int]:
        sample_total = prepared.current.node_count
        compiled = self._compile_targets(prepared, constraints)
        target_added = 0.0
        if compiled:
            first = compiled[0]
            delta = first.target.target - first.current
            sample_contribution = sum(
                template.sample_count * coefficient
                for template, coefficient in zip(prepared.templates, first.coefficients)
            )
            if sample_contribution and (sample_contribution > 0) == (delta > 0):
                target_added = max(0.0, delta * sample_total / sample_contribution)
            else:
                directional = [
                    abs(coefficient) for coefficient in first.coefficients
                    if coefficient and (coefficient > 0) == (delta > 0)
                ]
                if directional:
                    target_added = abs(delta) / max(directional)

        exact_quotas = [target_added * t.sample_count / sample_total for t in prepared.templates]
        counts = [math.floor(value) for value in exact_quotas]
        remainder = max(0, round(target_added) - sum(counts))
        order = sorted(
            range(len(counts)), key=lambda i: (exact_quotas[i] - counts[i], -i), reverse=True
        )
        for index in order[:remainder]:
            counts[index] += 1

        best_score = self._score(prepared, constraints, counts)
        if progress_callback is not None:
            progress_callback(counts)
        for _ in range(200):
            candidate_best: tuple[tuple[float, ...], list[int]] | None = None
            for index in range(len(counts)):
                for delta in (-1, 1):
                    if delta < 0 and counts[index] == 0:
                        continue
                    candidate = list(counts)
                    candidate[index] += delta
                    score = self._score(prepared, constraints, candidate)
                    if score < best_score and (candidate_best is None or score < candidate_best[0]):
                        candidate_best = (score, candidate)
            if candidate_best is None:
                break
            best_score, counts = candidate_best
            if progress_callback is not None:
                progress_callback(counts)
        return counts

    @staticmethod
    def _similarity_for_counts(
        compiled: list[_CompiledTarget], counts: list[int]
    ) -> float:
        similarities: list[float] = []
        for item in compiled:
            actual = item.current + sum(
                coefficient * count
                for coefficient, count in zip(item.coefficients, counts)
            )
            relative_deviation = abs(actual - item.target.target) / max(
                abs(item.target.target), 1
            )
            similarities.append(max(0.0, 1.0 - relative_deviation))
        if not similarities:
            return 0.0
        return 100.0 * sum(similarities) / len(similarities)

    def solution_similarity(
        self,
        prepared: DataFillPrepared,
        constraints: DataFillConstraints,
        counts: list[int],
    ) -> float:
        """Return the equal-weight target similarity for a candidate solution."""
        return self._similarity_for_counts(
            self._compile_targets(prepared, constraints), counts
        )

    def _score(
        self,
        prepared: DataFillPrepared,
        constraints: DataFillConstraints,
        counts: list[int],
    ) -> tuple[float, ...]:
        scores: list[float] = []
        for item in self._compile_targets(prepared, constraints):
            actual = item.current + sum(
                coefficient * count
                for coefficient, count in zip(item.coefficients, counts)
            )
            scores.append(float(abs(actual - item.target.target)))
        total = sum(counts)
        sample_total = prepared.current.node_count
        scores.append(float(sum(
            abs(sample_total * count - template.sample_count * total)
            for template, count in zip(prepared.templates, counts)
        )))
        scores.append(float(total))
        scores.append(float(sum((index + 1) * count for index, count in enumerate(counts))))
        return tuple(scores)

    def _build_result(
        self,
        prepared: DataFillPrepared,
        constraints: DataFillConstraints,
        counts: list[int],
        *,
        timed_out: bool,
        used_fallback: bool,
    ) -> DataFillResult:
        rackings = Counter(prepared.current.rackings)
        products = Counter(prepared.current.products)
        nodes = prepared.current.node_count + sum(counts)
        strings = prepared.current.string_count
        for template, count in zip(prepared.templates, counts):
            strings += template.contribution.string_count * count
            rackings.update({k: v * count for k, v in template.contribution.rackings.items()})
            products.update({k: v * count for k, v in template.contribution.products.items()})
        actual = QuantitySnapshot(nodes, strings, dict(rackings), dict(products))

        constraint_results = tuple(
            ConstraintResult(
                target_id=item.target.target_id,
                label=item.target.label,
                formula=format_data_fill_formula(item.target.terms),
                target=item.target.target,
                current=item.current,
                actual=self.target_value(actual, item.target),
            )
            for item in self._compile_targets(prepared, constraints)
        )
        sample_total = prepared.current.node_count
        template_results = tuple(
            TemplateResult(
                template.template_id,
                tuple(template.sample_names),
                template.sample_count,
                template.sample_count / sample_total,
                count,
            )
            for template, count in zip(prepared.templates, counts)
        )
        return DataFillResult(
            constraints=constraints,
            current=prepared.current,
            actual=actual,
            constraint_results=constraint_results,
            template_results=template_results,
            templates=prepared.templates,
            exact=all(item.absolute_deviation == 0 for item in constraint_results),
            timed_out=timed_out,
            used_fallback=used_fallback,
        )
