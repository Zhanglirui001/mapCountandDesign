"""Tree editing business logic."""

from __future__ import annotations

from dataclasses import dataclass

from ...models import Inv, Node, Project, Row


@dataclass(frozen=True)
class ParentContext:
    """Parent lookup for a selected item."""

    item_type: str
    item: object
    parent: object | None = None


class ProjectTreeService:
    """Manipulates the project hierarchy."""

    def resolve_parent_context(self, project: Project, item_type: str, item: object) -> ParentContext:
        if item_type == "project":
            return ParentContext(item_type=item_type, item=item)
        if item_type == "inv":
            return ParentContext(item_type=item_type, item=item, parent=project)
        if item_type == "node":
            for inv in project.invs:
                for node in inv.nodes:
                    if node is item:
                        return ParentContext(item_type=item_type, item=item, parent=inv)
        if item_type == "row":
            for inv in project.invs:
                for node in inv.nodes:
                    for row in node.rows:
                        if row is item:
                            return ParentContext(item_type=item_type, item=item, parent=node)
        raise ValueError("Unable to resolve selected item context")

    def create_child(self, project: Project, item_type: str, item: object) -> object | None:
        if item_type == "project" and isinstance(item, Project):
            last_name = item.invs[-1].name if item.invs else None
            created = Inv.new(last_name)
            item.invs.append(created)
            return created
        if item_type == "inv" and isinstance(item, Inv):
            last_name = item.nodes[-1].name if item.nodes else None
            created = Node.new(last_name)
            item.nodes.append(created)
            return created
        if item_type == "node" and isinstance(item, Node):
            last_name = item.rows[-1].name if item.rows else None
            created = Row.new(last_name)
            item.rows.append(created)
            return created
        return None

    def delete_item(self, project: Project, item_type: str, item: object) -> object | None:
        if item_type == "inv" and isinstance(item, Inv):
            idx, fallback = self._find_and_previous(item, project.invs, project)
            if idx is not None:
                del project.invs[idx]
            return fallback
        if item_type == "node" and isinstance(item, Node):
            context = self.resolve_parent_context(project, item_type, item)
            parent = context.parent
            if isinstance(parent, Inv):
                idx, fallback = self._find_and_previous(item, parent.nodes, parent)
                if idx is not None:
                    del parent.nodes[idx]
                return fallback
            return None
        if item_type == "row" and isinstance(item, Row):
            context = self.resolve_parent_context(project, item_type, item)
            parent = context.parent
            if isinstance(parent, Node):
                idx, fallback = self._find_and_previous(item, parent.rows, parent)
                if idx is not None:
                    del parent.rows[idx]
                return fallback
            return None
        return None

    @staticmethod
    def _find_and_previous(item: object, collection: list, fallback_parent: object) -> tuple[int | None, object]:
        """Find `item` in `collection` by identity and return (index, previous sibling or fallback_parent)."""
        for idx, elem in enumerate(collection):
            if elem is item:
                prev = collection[idx - 1] if idx > 0 else fallback_parent
                return (idx, prev)
        return (None, fallback_parent)

    def copy_item(self, item_type: str, item: object) -> Inv | Node | Row | None:
        if item_type == "inv" and isinstance(item, Inv):
            return item.clone()
        if item_type == "node" and isinstance(item, Node):
            return item.clone()
        if item_type == "row" and isinstance(item, Row):
            return item.clone()
        return None

    def paste_item(self, project: Project, item_type: str, item: object, clipboard: object) -> object | None:
        if item_type == "project" and isinstance(item, Project) and isinstance(clipboard, Inv):
            last_name = item.invs[-1].name if item.invs else None
            created = clipboard.clone()
            created.name = Inv.sequence_name(last_name)
            item.invs.append(created)
            return created
        if item_type == "inv" and isinstance(item, Inv) and isinstance(clipboard, Node):
            last_name = item.nodes[-1].name if item.nodes else None
            created = clipboard.clone()
            created.name = Node.sequence_name(last_name)
            item.nodes.append(created)
            return created
        if item_type == "node" and isinstance(item, Node) and isinstance(clipboard, Row):
            last_name = item.rows[-1].name if item.rows else None
            created = clipboard.clone()
            created.name = Row.sequence_name(last_name)
            item.rows.append(created)
            return created
        return None

