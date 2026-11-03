"""ViewModel for the History tab – displays the operation tree with folding."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ...application.version_manager import VersionManager
from ...i18n import tr
from .states import HistoryTabState, HistoryTreeNodeState


class HistoryTabViewModel(QObject):
    """Provides state and commands for the History tab.

    Supports folding of linear middle-node chains and rich summaries.
    """

    # Minimum number of middle nodes before a fold row is created.
    # Chains shorter than this are displayed in full without folding.
    MIN_FOLD_CHAIN: int = 3

    state_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._version_manager: VersionManager | None = None
        self._state = HistoryTabState()
        # Cache of raw (unfolded) tree nodes, keyed by node_id
        self._raw_nodes: dict[str, HistoryTreeNodeState] = {}
        # Folding state
        self._expanded_fold_id: str = ""   # e.g. "_fold_abc123"
        self._expanded_start: int = 0
        self._expanded_count: int = 0      # how many to show (0 = collapsed)
        # Fold chain tracking: node_id -> fold_id, fold_id -> chain node_ids
        self._node_to_fold: dict[str, str] = {}
        self._fold_chains: dict[str, list[str]] = {}
        # Search state
        self._search_results: list[str] = []
        self._current_search_index: int = -1
        self._search_target_node_id: str = ""

    @property
    def state(self) -> HistoryTabState:
        return self._state

    def set_version_manager(self, vm: VersionManager) -> None:
        self._version_manager = vm
        self._expanded_fold_id = ""
        self._expanded_start = 0
        self._expanded_count = 0
        self._raw_nodes.clear()
        self._node_to_fold.clear()
        self._fold_chains.clear()
        self._clear_search()
        self.refresh()

    def refresh(self) -> None:
        if self._version_manager is None:
            self._state = HistoryTabState()
            self.state_changed.emit(self._state)
            return

        # Rebuild raw node cache
        self._raw_nodes.clear()
        self._node_to_fold.clear()
        self._fold_chains.clear()
        tree_data = self._version_manager.get_tree()
        current_id = self._version_manager.current_id
        path_set = self._version_manager.get_path_set(current_id)

        raw_roots = tuple(
            self._build_raw_node(n, current_id) for n in tree_data
        )

        # Compute fold chains before folding (so search can find them)
        for r in raw_roots:
            self._compute_fold_chains(r)

        folded_tree = tuple(
            self._fold_node(r, path_set, current_id) for r in raw_roots
        )

        self._state = HistoryTabState(
            tree=folded_tree,
            current_node_id=current_id,
            current_description=self._version_manager.current_description,
            path_to_current=tuple(path_set),
            search_target_node_id=self._search_target_node_id,
        )
        self.state_changed.emit(self._state)

    # -- expand / collapse ------------------------------------------------

    def _resolve_fold_id(self, raw_fold_id: str, fold_count: int, fold_offset: int) -> tuple[str, int, int]:
        """Resolve a fold row ID into (base_fold_id, effective_offset, effective_total).

        Prefix/suffix fold rows (ending in _pre / _post) are mapped back to
        the parent chain so expansion works correctly.
        """
        if raw_fold_id.endswith("_pre"):
            base = raw_fold_id[:-4]  # strip "_pre"
            return (base, 0, fold_count)
        elif raw_fold_id.endswith("_post"):
            base = raw_fold_id[:-5]  # strip "_post"
            return (base, fold_offset, fold_count)
        else:
            return (raw_fold_id, 0, fold_count)

    def expand_fold_first(self, fold_id: str, fold_count: int, fold_offset: int = 0) -> None:
        base_id, offset, total = self._resolve_fold_id(fold_id, fold_count, fold_offset)
        self._expanded_fold_id = base_id
        self._expanded_start = offset
        self._expanded_count = min(10, total)
        self.refresh()

    def expand_fold_last(self, fold_id: str, fold_count: int, fold_offset: int = 0) -> None:
        base_id, offset, total = self._resolve_fold_id(fold_id, fold_count, fold_offset)
        start = offset + max(0, total - 10)
        self._expanded_fold_id = base_id
        self._expanded_start = start
        self._expanded_count = min(10, total - (start - offset))
        self.refresh()

    def expand_fold_at(self, fold_id: str, index: int, fold_count: int, fold_offset: int = 0) -> None:
        base_id, offset, total = self._resolve_fold_id(fold_id, fold_count, fold_offset)
        index = max(0, min(index, total - 1))
        count = min(10, total - index)
        self._expanded_fold_id = base_id
        self._expanded_start = offset + index
        self._expanded_count = count
        self.refresh()

    def collapse_expanded_fold(self) -> None:
        self._expanded_fold_id = ""
        self._expanded_start = 0
        self._expanded_count = 0
        self.refresh()

    # -- search ----------------------------------------------------------

    def search(self, query: str) -> int:
        """Search raw nodes for *query* in description and summary.

        Returns the number of matching nodes.  Does **not** automatically
        navigate — call :meth:`next_result` afterwards.
        """
        q = query.strip().lower()
        self._search_results.clear()
        self._current_search_index = -1
        if not q:
            self._search_target_node_id = ""
            self.refresh()
            return 0
        for node_id, node in self._raw_nodes.items():
            if node.is_fold_row:
                continue
            if q in node.description.lower() or q in node.summary.lower():
                self._search_results.append(node_id)
        return len(self._search_results)

    def next_result(self) -> str | None:
        """Advance to the next search result.

        Expands any enclosing fold so the node becomes visible, then
        triggers a refresh.  Returns the target *node_id*.
        """
        if not self._search_results:
            return None
        self._current_search_index = (
            (self._current_search_index + 1) % len(self._search_results)
        )
        target = self._search_results[self._current_search_index]
        self._ensure_node_visible(target)
        return target

    def prev_result(self) -> str | None:
        """Go back to the previous search result."""
        if not self._search_results:
            return None
        if self._current_search_index <= 0:
            self._current_search_index = len(self._search_results) - 1
        else:
            self._current_search_index -= 1
        target = self._search_results[self._current_search_index]
        self._ensure_node_visible(target)
        return target

    @property
    def search_result_count(self) -> int:
        return len(self._search_results)

    @property
    def search_result_index(self) -> int:
        """1-based index of the current result (0 when empty)."""
        if not self._search_results:
            return 0
        return self._current_search_index + 1

    def _ensure_node_visible(self, node_id: str) -> None:
        """If *node_id* is inside a folded chain, expand that fold to show it."""
        fold_id = self._node_to_fold.get(node_id)
        if fold_id and fold_id != self._expanded_fold_id:
            chain = self._fold_chains.get(fold_id, [])
            if node_id in chain:
                idx = chain.index(node_id)
                # Show up to 10 nodes, centering the target when possible
                start = max(0, idx - 3)
                count = min(10, len(chain) - start)
                self._expanded_fold_id = fold_id
                self._expanded_start = start
                self._expanded_count = count
        self._search_target_node_id = node_id
        self.refresh()

    def _clear_search(self) -> None:
        """Reset search state without triggering a refresh."""
        self._search_results.clear()
        self._current_search_index = -1
        self._search_target_node_id = ""

    # -- raw tree building ------------------------------------------------

    def _build_raw_node(
        self,
        data: dict,
        current_id: str,
    ) -> HistoryTreeNodeState:
        """Build a HistoryTreeNodeState tree non‑recursively (post‑order).

        Uses an explicit stack so that arbitrarily deep trees never
        overflow the C call stack.
        """
        root_id: str = data["id"]
        built: dict[str, HistoryTreeNodeState] = {}
        # Each stack entry is (node_data, state): 0 = first visit, 1 = children ready
        stack: list[tuple[dict, int]] = [(data, 0)]

        while stack:
            node_data, state = stack.pop()
            nid: str = node_data["id"]

            if state == 0:
                # Post‑order: push self back, then children
                stack.append((node_data, 1))
                raw_children: list[dict] = node_data.get("children", [])
                # Reverse so that original order is preserved on pop
                for child in reversed(raw_children):
                    stack.append((child, 0))
            else:
                raw_children = node_data.get("children", [])
                children = tuple(built[c["id"]] for c in raw_children)
                node = HistoryTreeNodeState(
                    node_id=nid,
                    description=node_data["description"],
                    summary=node_data.get("summary", ""),
                    timestamp=node_data.get("timestamp", 0.0),
                    is_current=nid == current_id,
                    is_locked=bool(node_data.get("is_locked", False)),
                    children=children,
                )
                built[nid] = node
                self._raw_nodes[nid] = node

        return built[root_id]

    # -- folding logic ---------------------------------------------------

    @staticmethod
    def _is_middle_node(node: HistoryTreeNodeState) -> bool:
        """Non-root, non-current, exactly 1 child."""
        if node.node_id == "_root":
            return False
        if node.is_current:
            return False
        if len(node.children) != 1:
            return False
        return True

    def _compute_fold_chains(self, node: HistoryTreeNodeState) -> None:
        """Walk the raw tree and record all middle-node chains for search.

        Populates ``_node_to_fold`` and ``_fold_chains`` so that
        :meth:`_ensure_node_visible` can later expand the correct fold.
        """
        if self._is_middle_node(node):
            chain_ids: list[str] = [node.node_id]
            cursor = node
            while self._is_middle_node(cursor.children[0]):
                cursor = cursor.children[0]
                chain_ids.append(cursor.node_id)
            if len(chain_ids) >= self.MIN_FOLD_CHAIN:
                fold_id = f"_fold_{chain_ids[0]}"
                self._fold_chains[fold_id] = chain_ids
                for nid in chain_ids:
                    self._node_to_fold[nid] = fold_id
            # Recurse into the anchor
            self._compute_fold_chains(cursor.children[0])
        else:
            for child in node.children:
                self._compute_fold_chains(child)

    def _fold_node(
        self,
        node: HistoryTreeNodeState,
        path_set: set[str],
        current_id: str,
    ) -> HistoryTreeNodeState:
        """Recursively fold *node* and its descendants."""
        if not self._is_middle_node(node):
            # Not a middle node — just fold children
            folded_children = tuple([
                self._fold_node(c, path_set, current_id) for c in node.children
            ])
            return HistoryTreeNodeState(
                node_id=node.node_id,
                description=node.description,
                summary=node.summary,
                timestamp=node.timestamp,
                is_current=node.is_current,
                is_locked=node.is_locked,
                children=folded_children,
            )

        # This node is a middle node. Follow the linear chain.
        chain_ids: list[str] = [node.node_id]
        cursor = node
        while self._is_middle_node(cursor.children[0]):
            cursor = cursor.children[0]
            chain_ids.append(cursor.node_id)

        # cursor.children[0] is the anchor (current / branch / leaf)
        anchor = cursor.children[0]

        # Recursively fold anchor's children
        folded_anchor_children = tuple([
            self._fold_node(c, path_set, current_id) for c in anchor.children
        ])
        folded_anchor = HistoryTreeNodeState(
            node_id=anchor.node_id,
            description=anchor.description,
            summary=anchor.summary,
            timestamp=anchor.timestamp,
            is_current=anchor.is_current,
            is_locked=anchor.is_locked,
            children=folded_anchor_children,
        )

        total = len(chain_ids)
        fold_id = f"_fold_{chain_ids[0]}"

        # -- Short chains: display in full without a fold row -----------------
        if total < self.MIN_FOLD_CHAIN and fold_id != self._expanded_fold_id:
            # Chain all middle nodes + anchor linearly, no fold wrapper.
            result = folded_anchor
            for nid in reversed(chain_ids):
                raw = self._raw_nodes[nid]
                result = HistoryTreeNodeState(
                    node_id=raw.node_id,
                    description=raw.description,
                    summary=raw.summary,
                    timestamp=raw.timestamp,
                    is_current=raw.is_current,
                    is_locked=raw.is_locked,
                    children=(result,),
                )
            return result

        if fold_id == self._expanded_fold_id:
            # Show a slice of the chain
            return self._build_expanded_chain(
                chain_ids, folded_anchor, fold_id, total, path_set, current_id
            )
        else:
            # Fully collapsed
            fold_row = HistoryTreeNodeState(
                node_id=fold_id,
                description=tr("history_tab.fold.collapsed", "{count} nodes folded").format(count=total),
                summary="",
                timestamp=self._raw_nodes[chain_ids[0]].timestamp,
                is_current=False,
                is_fold_row=True,
                fold_count=total,
                fold_start_index=0,
                children=(folded_anchor,),
            )
            return fold_row

    def _build_expanded_chain(
        self,
        chain_ids: list[str],
        anchor: HistoryTreeNodeState,
        fold_id: str,
        total: int,
        path_set: set[str],
        current_id: str,
    ) -> HistoryTreeNodeState:
        """Build the display tree when a fold is expanded, showing a slice."""
        start = self._expanded_start
        count = self._expanded_count

        # Absorb small prefix / suffix remainders into the visible range
        # so we don't create pointless fold rows for 1-2 nodes.
        remaining = total - (start + count)
        if 0 < remaining < self.MIN_FOLD_CHAIN:
            count += remaining
            remaining = 0
        if 0 < start < self.MIN_FOLD_CHAIN:
            count += start
            start = 0
            remaining = total - (start + count)

        # Build the list of display nodes for the visible slice
        display_nodes: list[HistoryTreeNodeState] = []

        # Prefix fold row
        if start > 0:
            prefix_id = f"{fold_id}_pre"
            display_nodes.append(
                HistoryTreeNodeState(
                    node_id=prefix_id,
                    description=tr("history_tab.fold.collapsed", "{count} nodes folded").format(count=start),
                    summary="",
                    timestamp=self._raw_nodes[chain_ids[0]].timestamp,
                    is_current=False,
                    is_fold_row=True,
                    fold_count=start,
                    fold_start_index=0,
                    children=tuple(),
                )
            )

        # Visible chain nodes: children are set to empty — the chaining
        # loop below (reversed over display_nodes) wires up the correct
        # parent → child links, so any children here would be discarded.
        for j in range(start, start + count):
            raw_node = self._raw_nodes[chain_ids[j]]
            display_nodes.append(
                HistoryTreeNodeState(
                    node_id=raw_node.node_id,
                    description=raw_node.description,
                    summary=raw_node.summary,
                    timestamp=raw_node.timestamp,
                    is_current=raw_node.is_current,
                    is_locked=raw_node.is_locked,
                    children=tuple(),
                )
            )

        # Suffix fold row
        if remaining > 0:
            suffix_id = f"{fold_id}_post"
            display_nodes.append(
                HistoryTreeNodeState(
                    node_id=suffix_id,
                    description=tr("history_tab.fold.collapsed", "{count} nodes folded").format(count=remaining),
                    summary="",
                    timestamp=self._raw_nodes[chain_ids[start + count]].timestamp,
                    is_current=False,
                    is_fold_row=True,
                    fold_count=remaining,
                    fold_start_index=start + count,
                    children=tuple(),
                )
            )

        # Chain them: display_nodes[0] → display_nodes[1] → ... → anchor
        result = anchor
        for dn in reversed(display_nodes):
            result = HistoryTreeNodeState(
                node_id=dn.node_id,
                description=dn.description,
                summary=dn.summary,
                timestamp=dn.timestamp,
                is_current=dn.is_current,
                is_locked=dn.is_locked,
                is_fold_row=dn.is_fold_row,
                fold_count=dn.fold_count,
                fold_start_index=dn.fold_start_index,
                children=(result,),
            )

        return result
