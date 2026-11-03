"""Tree expansion and ancestor-reveal rules for the Count tab."""

from __future__ import annotations

from dataclasses import dataclass

from ....models import Inv, Node, Project, Row


def tree_item_key(item: object) -> str:
    return f"{item.__class__.__name__.lower()}:{id(item)}"


@dataclass(frozen=True)
class ExpansionResult:
    expanded_ids: set[str]
    selected_id: str


class TreeExpansionController:
    """Apply Alt+1/2/3 tree rules without Qt or ViewModel state."""

    @staticmethod
    def key_for(item: object) -> str:
        return tree_item_key(item)

    def expand(
        self,
        project: Project,
        item_type: str,
        item: object,
        level: int,
        expanded_ids: set[str],
        selected_id: str,
    ) -> ExpansionResult:
        expanded = set(expanded_ids)
        if item_type == "project":
            expanded = self._expand_project(project, level, expanded)
        elif item_type == "inv":
            expanded, selected_id = self._expand_inv(
                project,
                item,
                level,
                expanded,
                selected_id,
            )
        elif item_type == "node":
            expanded, selected_id = self._expand_node(
                project,
                item,
                level,
                expanded,
                selected_id,
            )
        elif item_type == "row":
            expanded, selected_id = self._expand_row(
                project,
                item,
                level,
                expanded,
                selected_id,
            )
        return ExpansionResult(expanded, selected_id)

    def reveal(
        self,
        project: Project,
        item_type: str,
        item: object,
        expanded_ids: set[str],
    ) -> set[str]:
        expanded = set(expanded_ids)
        expanded.add(self.key_for(project))
        parent_inv = self.find_parent_inv(project, item)
        if parent_inv is not None:
            expanded.add(self.key_for(parent_inv))
        if item_type == "row":
            parent_node = self.find_parent_node(project, item)
            if parent_node is not None:
                expanded.add(self.key_for(parent_node))
        return expanded

    def _expand_project(
        self,
        project: Project,
        level: int,
        expanded: set[str],
    ) -> set[str]:
        project_key = self.key_for(project)
        inv_keys = {self.key_for(inv) for inv in project.invs}
        node_keys = self._all_node_keys(project)
        if level == 1:
            invs_collapsed = not (expanded & inv_keys)
            nodes_collapsed = not (expanded & node_keys)
            if invs_collapsed and nodes_collapsed:
                if project_key in expanded:
                    expanded.discard(project_key)
                else:
                    expanded.add(project_key)
            else:
                expanded = {project_key}
        elif level == 2:
            invs_all_expanded = bool(inv_keys) and inv_keys <= expanded
            invs_all_collapsed = not (expanded & inv_keys)
            rows_hidden = not (expanded & node_keys)
            if (invs_all_expanded or invs_all_collapsed) and rows_hidden:
                if invs_all_expanded:
                    expanded -= inv_keys
                else:
                    expanded.add(project_key)
                    expanded.update(inv_keys)
            else:
                expanded = {project_key} | inv_keys
        elif level == 3:
            nodes_all_expanded = bool(node_keys) and node_keys <= expanded
            nodes_all_collapsed = not (expanded & node_keys)
            if nodes_all_expanded or nodes_all_collapsed:
                if nodes_all_expanded:
                    expanded -= node_keys
                else:
                    expanded.add(project_key)
                    expanded.update(inv_keys)
                    expanded.update(node_keys)
            else:
                expanded = {project_key} | inv_keys | node_keys
        return expanded

    def _expand_inv(
        self,
        project: Project,
        item: object,
        level: int,
        expanded: set[str],
        selected_id: str,
    ) -> tuple[set[str], str]:
        if level == 1:
            return set(), self.key_for(project)
        inv = item
        project_key = self.key_for(project)
        inv_key = self.key_for(inv)
        node_keys = {self.key_for(node) for node in inv.nodes}
        if level == 2:
            rows_hidden = not (expanded & node_keys)
            if rows_hidden:
                if inv_key in expanded:
                    expanded.discard(inv_key)
                    expanded -= node_keys
                else:
                    expanded.add(project_key)
                    expanded.add(inv_key)
            else:
                expanded.add(project_key)
                expanded.add(inv_key)
                expanded -= node_keys
        elif level == 3:
            nodes_all_expanded = bool(node_keys) and node_keys <= expanded
            nodes_all_collapsed = not (expanded & node_keys)
            if nodes_all_expanded or nodes_all_collapsed:
                if nodes_all_expanded:
                    expanded -= node_keys
                else:
                    expanded.add(project_key)
                    expanded.add(inv_key)
                    expanded.update(node_keys)
            else:
                expanded.add(project_key)
                expanded.add(inv_key)
                expanded.update(node_keys)
        return expanded, selected_id

    def _expand_node(
        self,
        project: Project,
        item: object,
        level: int,
        expanded: set[str],
        selected_id: str,
    ) -> tuple[set[str], str]:
        if level == 1:
            return set(), self.key_for(project)
        node = item
        parent_inv = self.find_parent_inv(project, node)
        if level == 2:
            if parent_inv is None:
                return expanded, selected_id
            return {self.key_for(project)}, self.key_for(parent_inv)
        if level == 3:
            node_key = self.key_for(node)
            if node_key in expanded:
                expanded.discard(node_key)
            else:
                expanded.add(self.key_for(project))
                if parent_inv is not None:
                    expanded.add(self.key_for(parent_inv))
                expanded.add(node_key)
        return expanded, selected_id

    def _expand_row(
        self,
        project: Project,
        item: object,
        level: int,
        expanded: set[str],
        selected_id: str,
    ) -> tuple[set[str], str]:
        if level == 1:
            return set(), self.key_for(project)
        row = item
        parent_inv = self.find_parent_inv(project, row)
        if level == 2:
            if parent_inv is None:
                return expanded, selected_id
            return {self.key_for(project)}, self.key_for(parent_inv)
        if level == 3:
            parent_node = self.find_parent_node(project, row)
            if parent_node is None:
                return expanded, selected_id
            expanded = {self.key_for(project)}
            if parent_inv is not None:
                expanded.add(self.key_for(parent_inv))
            return expanded, self.key_for(parent_node)
        return expanded, selected_id

    def _all_node_keys(self, project: Project) -> set[str]:
        return {
            self.key_for(node)
            for inv in project.invs
            for node in inv.nodes
        }

    @staticmethod
    def find_parent_inv(project: Project, item: object) -> Inv | None:
        for inv in project.invs:
            for node in inv.nodes:
                if node is item or any(row is item for row in node.rows):
                    return inv
        return None

    @staticmethod
    def find_parent_node(project: Project, row: object) -> Node | None:
        for inv in project.invs:
            for node in inv.nodes:
                if any(candidate is row for candidate in node.rows):
                    return node
        return None
