"""编码/命名工具函数 — 为部件和产品生成编号。"""

from __future__ import annotations

from ...models.assembled_product import AssembledProduct
from ...models.part import Part


def _to_alpha(n: int) -> str:
    """将整数 n（0 起始）转换为 Excel 列式字母序列。

    0 -> A, 1 -> B, ..., 25 -> Z, 26 -> AA, 27 -> AB, ...
    """
    result = ""
    while True:
        result = chr(ord('A') + n % 26) + result
        n = n // 26 - 1
        if n < 0:
            break
    return result


def _get_next_semi_code(semi_parts: list[Part]) -> str:
    """在已有 semi_parts 中查找最大 SE- 编号，返回递增后的新编号。"""
    max_num = 0
    for sp in semi_parts:
        if sp.code.startswith("SE-"):
            digits = "".join(ch for ch in sp.code[3:] if ch.isdigit())
            if digits:
                max_num = max(max_num, int(digits))
    return f"SE-{max_num + 1}"


def _get_next_alpha_code(
    products: list[AssembledProduct], prefix: str
) -> str:
    """在已有产品列表中查找最大前缀+字母编号，返回递增后的新编号。

    例如 prefix="H" → "HA", "HB", ..., "HZ", "HAA", "HAB", ...
    """
    max_index = -1
    prefix_len = len(prefix)
    for prod in products:
        code = prod.code
        if code.startswith(prefix):
            alpha_part = code[prefix_len:]
            if alpha_part and alpha_part.isalpha() and alpha_part.isupper():
                # 将字母序列转为索引
                index = 0
                for ch in alpha_part:
                    index = index * 26 + (ord(ch) - ord('A') + 1)
                index -= 1  # 转为 0 起始
                max_index = max(max_index, index)
    return prefix + _to_alpha(max_index + 1)


def _get_digits(s: str) -> int:
    """返回字符串中的数字部分，转为整数。"""
    digits = "".join(ch for ch in s if ch.isdigit())
    return int(digits) if digits else 0


def _get_new_counter(parts: list[Part], category: str, code_prefix: str = "", start_from: int = 0) -> str:
    """在已有 parts 中查找同 category 且同 code 前缀的最大编号，返回递增后的新编号字符串。

    Args:
        parts: 已有部件列表。
        category: 部件类别。
        code_prefix: code 前缀，仅匹配以此前缀开头的部件；为空时匹配所有同 category 部件。
        start_from: 起始编号的基准值（递增前的基数），默认为 0，即第一个编号为 1。
    """
    counter = start_from
    for part in parts:
        if part.category.lower() == category.lower():
            if code_prefix and not part.code.startswith(code_prefix):
                continue
            counter = max(counter, _get_digits(part.code))
    counter += 1
    return str(counter)


def _get_new_alpha_counter(parts: list[Part], category: str, code_prefix: str = "") -> str:
    """在已有 parts 中查找同 category 且同 code 前缀的最大字母编号，返回递增后的字母编号。

    与 _get_new_counter 类似，但编号使用字母序列：A, B, ..., Z, AA, AB, ...

    Args:
        parts: 已有部件列表。
        category: 部件类别。
        code_prefix: code 前缀，仅匹配以此前缀开头的部件。

    Returns:
        递增后的字母编号，如 "A", "B", ..., "AA", "AB"。
    """
    max_index = -1
    prefix_len = len(code_prefix)
    for part in parts:
        if part.category.lower() != category.lower():
            continue
        code = part.code
        if not code.startswith(code_prefix):
            continue
        alpha_part = code[prefix_len:]
        if alpha_part and alpha_part.isalpha() and alpha_part.isupper():
            index = 0
            for ch in alpha_part:
                index = index * 26 + (ord(ch) - ord('A') + 1)
            index -= 1
            max_index = max(max_index, index)
    return _to_alpha(max_index + 1)

# ---------------------------------------------------------------------------
# 半成品命名 — 根据半成品结构检测前缀与极性
# ---------------------------------------------------------------------------

def _detect_semi_prefix(semi_part: Part) -> str:
    """检测半成品应使用的命名前缀。

    根据 semi_list 中的内容判断半成品类型和极性，返回对应的前缀字符串。

    检测规则：
    - semi_list[0].category == "T Kit"        → IBEX 外层（带 T 转接件）→ 前缀 "T"
    - semi_list[0].category == "OverMolding"   → LYNX 外层（带 overmold）→ 前缀 "O"
    - semi_list 中存在 category == "AC" 的部件  → LYNX 内层（branch semi）→ 前缀 "E"
    - 其他                                       → IBEX 内层               → 前缀 "B"

    极性通过递归查找 semi_list 中第一个导线部件的描述判断：
    - 描述含 "Red"  → "P"
    - 描述含 "Black" → "N"
    - 默认为 "P"

    Returns:
        前缀字符串，如 "TP", "TN", "BP", "BN", "OP", "ON", "EP", "EN"。
    """
    # 1) 递归检测极性：在 semi_list 树中查找第一个 wire
    def _find_polarity(part: Part) -> str | None:
        for p in part.semi_list:
            if p.category == "Wire" and p.description:
                if "Red" in p.description:
                    return "P"
                elif "Black" in p.description:
                    return "N"
            # 如果子部件也是半成品，递归查找
            if p.semi_or_not and p.semi_list:
                result = _find_polarity(p)
                if result is not None:
                    return result
        return None

    polarity_letter = _find_polarity(semi_part) or "P"

    if not semi_part.semi_list:
        return "B" + polarity_letter  # 空半成品视为 IBEX 内层

    first = semi_part.semi_list[0]

    # 2) 检测外层类型
    if first.category == "T Kit":
        return "T" + polarity_letter       # IBEX 外层（带 T 转接件）
    if first.category == "OverMolding":
        return "O" + polarity_letter       # LYNX 外层（带 overmold）

    # 3) 检测是否为 LYNX 内层（含 Cu-Al 转接件）
    for p in semi_part.semi_list:
        if p.category == "AC":
            return "E" + polarity_letter   # LYNX 内层

    # 4) 默认为 IBEX 内层
    return "B" + polarity_letter


def _get_next_semi_code_by_prefix(semi_parts: list[Part], prefix: str) -> str:
    """在已有 semi_parts 中查找最大 prefix+数字 编号，返回递增后的新编号。

    例如 prefix="TP" → "TP1", "TP2", ...

    Args:
        semi_parts: 已有半成品列表。
        prefix: 命名前缀，如 "TP", "TN", "BP", "BN", "OP", "ON", "EP", "EN"。

    Returns:
        新编号字符串，如 "TP1"。
    """
    max_num = 0
    prefix_len = len(prefix)
    for sp in semi_parts:
        if sp.code.startswith(prefix):
            digits = "".join(ch for ch in sp.code[prefix_len:] if ch.isdigit())
            if digits:
                max_num = max(max_num, int(digits))
    return f"{prefix}{max_num + 1}"

# ---------------------------------------------------------------------------
# Wire 部件字母前缀 — 根据材质和股数动态分配字母
# ---------------------------------------------------------------------------

# (wire_type, material, strands) → 字母前缀（不含末尾连字符）
_wire_prefix_map: dict[tuple[str, str, str], str] = {}
# 下一个可分配的字母索引（0 起始），仅用于非 LYNX_Branch_Al 类型
_next_wire_letter_index: int = 0


def _to_alpha_skip_e(n: int) -> str:
    """将整数 n 转换为字母序列，跳过 'E'（预留给 LYNX_Branch_Al）。

    0 -> A, 1 -> B, 2 -> C, 3 -> D, 4 -> F, 5 -> G, ...
    """
    if n < 4:
        return _to_alpha(n)
    return _to_alpha(n + 1)


def _get_wire_prefix(wire_type: str, material: str, strands: str) -> str:
    """根据线缆类型、材质和股数获取唯一的字母前缀。

    LYNX_Branch_Al 类型始终使用字母 "E"，不同变体通过数字区分。
    其他类型按首次遇到顺序分配字母（A-D, F-Z，跳过 E）。
    同一 (wire_type, material, strands) 组合始终返回相同前缀。

    Args:
        wire_type: 线缆类型，如 "normal"、"LYNX_Branch_Al" 等。
        material: 材质，如 "Cu"、"Al"。
        strands: 股数，如 "7"、"48"、"none"。

    Returns:
        字母前缀（含末尾连字符），如 "A-"、"B-"、"E-"。
    """
    global _next_wire_letter_index
    key = (wire_type, material, strands)
    if key not in _wire_prefix_map:
        if wire_type == "LYNX_Branch_Al":
            letter = "E"
        else:
            letter = _to_alpha_skip_e(_next_wire_letter_index)
            _next_wire_letter_index += 1
        _wire_prefix_map[key] = letter
    return _wire_prefix_map[key] + "-"


def _reset_wire_prefixes() -> None:
    """重置 wire 字母前缀映射（每次产品生成前调用）。"""
    global _wire_prefix_map, _next_wire_letter_index
    _wire_prefix_map.clear()
    _next_wire_letter_index = 0
