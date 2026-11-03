"""产品生成服务 — 根据 Project 生成所有产品的部件列表。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ...models.length import Length

from .codegen import (
    _get_next_alpha_code,
    _detect_semi_prefix,
    _get_next_semi_code_by_prefix,
    _reset_wire_prefixes,
)
from .dedup import (
    _add_assembled_product_with_dedup,
    _assembled_products_equal,
)
from .gap_extender import (
    _add_gap_extender_product_with_dedup,
    _make_gap_extender_product,
)
from .homerun_clustering import cluster_homerun_extender_products
from .homerun_extender import _make_homerun_extender_product
from .ibex import _make_ibex_product
from .lynx import _make_lynx_product
from .result import (
    GeneratedPresetProducts,
    GeneratedRackingInstance,
    ProductGenerationResult,
)

if TYPE_CHECKING:
    from ...models import (
        Node,
        ProductPreset,
        Project,
        Row,
        Wire,
    )


class ProductGenerationService:
    """产品生成服务 — 根据 Project 生成所有产品的部件列表。"""

    def build_products(
        self,
        project: Project,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> ProductGenerationResult:
        """从 Project 构建所有产品。

        若提供 progress_callback，则在处理每个 Row 后调用，
        参数为 (已处理 Row 数, 总 Row 数)。
        """
        _reset_wire_prefixes()
        result = ProductGenerationResult()

        # 计算项目中总的 Row 数
        total_rows = sum(
            1
            for inv in project.invs
            for node in inv.nodes
            for _ in node.rows
        )
        processed_rows = 0

        # 查找 LYNX preset
        lynx_preset: ProductPreset | None = None
        for preset in project.product_presets:
            if preset.product_type == "LYNX":
                lynx_preset = preset
                break

        for inv in project.invs:
            # 继承链：Project → Inv → Node
            # LYNX preset 优先级：Node > Inv > Project
            lynx_preset_name: str | None = project.lynx_preset
            racking_pitch: Length | None = project.racking_pitch

            if inv.lynx_preset is not None:
                lynx_preset_name = inv.lynx_preset
            if inv.racking_pitch is not None:
                racking_pitch = inv.racking_pitch

            for node in inv.nodes:
                _lynx_name = lynx_preset_name
                _rp = racking_pitch
                if node.lynx_preset is not None:
                    _lynx_name = node.lynx_preset
                if node.racking_pitch is not None:
                    _rp = node.racking_pitch

                # 根据 lynx_preset 名称查找对应的 ProductPreset
                _lynx_preset: ProductPreset | None = None
                if _lynx_name is not None:
                    for pp in project.product_presets:
                        if pp.name == _lynx_name and pp.product_type == "LYNX":
                            _lynx_preset = pp
                            break

                # 获取 cluster_wire：从 lynx_preset 中获取
                _cw: Wire | None = _lynx_preset.cluster_wire if _lynx_preset else None

                # 生成 LYNX 产品（每个 Node 一对）
                if _lynx_preset is not None and _cw is not None and _rp is not None:
                    pos_lynx, neg_lynx = _make_lynx_product(
                        result.semi_parts, result.parts,
                        _lynx_preset, node, _rp, _cw, project,
                    )
                    # 填充 LYNX 对应关系表元数据（其余字段在 _make_lynx_product 中填充）
                    for lynx_prod in (pos_lynx, neg_lynx):
                        lynx_prod.inv_name = inv.name
                        result.all_products.append(lynx_prod)
                    _add_assembled_product_with_dedup(
                        result.lynx_products, pos_lynx,
                    )
                    _add_assembled_product_with_dedup(
                        result.lynx_products, neg_lynx,
                    )

                # 生成 IBEX 产品
                row_num = 0
                for row in node.rows:
                    row_num += 1
                    # Each racking entry generates its own chain of IBEX products
                    for entry_index, entry in enumerate(row.racking_entries):
                        entry_rackings = project.get_rackings(entry.rackings)
                        if entry_rackings is None:
                            continue
                        racking_instance = GeneratedRackingInstance(
                            inv_name=inv.name,
                            node_name=node.name,
                            row_name=row.name if row.name else f"Row{row_num}",
                            entry_index=entry_index,
                            rackings_name=entry.rackings,
                            preset_names=tuple(entry_rackings.product_presets),
                        )
                        result.racking_instances.append(racking_instance)
                        if _rp is None:
                            continue
                        extender_length = Length("0mm")
                        # 记录已遍历的 preset 名称，用于计算"跨过的产品"
                        seen_presets: list[str] = []
                        rtr = abs(node.box_position - row_num)
                        for preset_index, preset_name in enumerate(
                            entry_rackings.product_presets
                        ):
                            product_preset = project.get_product_preset(preset_name)
                            if product_preset is None:
                                continue
                            if product_preset.product_type not in (
                                "IBEX", "Homerun_Extender", "Placeholder"
                            ):
                                racking_instance.preset_products.append(
                                    GeneratedPresetProducts(
                                        preset_index=preset_index,
                                        preset_name=product_preset.name,
                                        product_type=product_preset.product_type,
                                    )
                                )
                                seen_presets.append(product_preset.name)
                                continue
                            whip_len = (
                                _rp * rtr
                                + entry.offset
                            )
                            # LYNX 项目中 IBEX/Extender 的 whip_len 固定为 0
                            if project.project_type == "LYNX":
                                whip_len = Length("0mm")
                            # 计算"跨过的产品"：racketings.product_presets 中在当前 preset 之前的 preset 名称
                            crossed = ", ".join(seen_presets) if seen_presets else ""
                            # Placeholder 不生成产品，只把 total length 累加给后续产品。
                            if (
                                product_preset.product_type == "Placeholder"
                                or product_preset.branch_count == 0
                            ):
                                racking_instance.preset_products.append(
                                    GeneratedPresetProducts(
                                        preset_index=preset_index,
                                        preset_name=product_preset.name,
                                        product_type=product_preset.product_type,
                                    )
                                )
                                seen_presets.append(product_preset.name)
                                extender_length = (
                                    extender_length
                                    + product_preset.product_total_length
                                )
                                continue

                            # 判断该 preset 实际生成的产品类型
                            is_homerun = (
                                product_preset.product_type == "Homerun_Extender"
                                or product_preset.branch_count <= 1
                            )
                            actual_type = "Homerun_Extender" if is_homerun else "IBEX"

                            def _attach_metadata(prod: object) -> None:
                                prod.product_type = actual_type
                                prod.inv_name = inv.name
                                prod.node_name = node.name
                                prod.row_name = row.name if row.name else f"Row{row_num}"
                                prod.rtr_count = rtr
                                prod.racking_pitch_str = _rp.length_str
                                prod.racking_pitch_ft = round(_rp.to_ft(), 4)
                                prod.offset_str = entry.offset.length_str
                                prod.offset_ft = round(entry.offset.to_ft(), 4)
                                prod.whip_ft = round(whip_len.to_ft(), 4)
                                prod.rackings_name = entry.rackings
                                prod.preset_name = product_preset.name
                                prod.extender_ft = round(extender_length.to_ft(), 4)
                                prod.crossed_presets = crossed
                                prod.preset_remarks = product_preset.remarks
                                result.all_products.append(prod)

                            if is_homerun:
                                pos_he, neg_he = _make_homerun_extender_product(
                                    result.parts,
                                    product_preset, whip_len, extender_length,
                                )
                                racking_instance.preset_products.append(
                                    GeneratedPresetProducts(
                                        preset_index=preset_index,
                                        preset_name=product_preset.name,
                                        product_type=actual_type,
                                        positive=pos_he,
                                        negative=neg_he,
                                    )
                                )
                                if pos_he is not None:
                                    _attach_metadata(pos_he)
                                    _add_assembled_product_with_dedup(
                                        result.homerun_extender_products, pos_he,
                                    )
                                if neg_he is not None:
                                    _attach_metadata(neg_he)
                                    _add_assembled_product_with_dedup(
                                        result.homerun_extender_products, neg_he,
                                    )
                            else:
                                pos_ibex, neg_ibex = _make_ibex_product(
                                    result.semi_parts, result.parts,
                                    product_preset, whip_len, extender_length,
                                )
                                racking_instance.preset_products.append(
                                    GeneratedPresetProducts(
                                        preset_index=preset_index,
                                        preset_name=product_preset.name,
                                        product_type=actual_type,
                                        positive=pos_ibex,
                                        negative=neg_ibex,
                                    )
                                )
                                if pos_ibex is not None:
                                    _attach_metadata(pos_ibex)
                                    _add_assembled_product_with_dedup(
                                        result.ibex_products, pos_ibex,
                                    )
                                if neg_ibex is not None:
                                    _attach_metadata(neg_ibex)
                                    _add_assembled_product_with_dedup(
                                        result.ibex_products, neg_ibex,
                                    )
                            seen_presets.append(product_preset.name)
                            extender_length = (
                                extender_length
                                + product_preset.product_total_length
                            )
                    # 该 Row 的所有 entries/presets 处理完毕
                    processed_rows += 1
                    if progress_callback is not None:
                        progress_callback(processed_rows, total_rows)

        # 主循环结束后，确保进度到达 100%
        if progress_callback is not None and processed_rows < total_rows:
            progress_callback(total_rows, total_rows)

        # ---- 生成 Gap Extender 产品 ----
        # 扫描所有 inv/node/row，从 rackings 的 gap_extenders 列表中读取
        for inv in project.invs:
            for node in inv.nodes:
                row_num = 0
                for row in node.rows:
                    row_num += 1
                    for entry in row.racking_entries:
                        entry_rackings = project.get_rackings(entry.rackings)
                        if entry_rackings is None:
                            continue
                        for ext_name, ext_qty in entry_rackings.gap_extenders:
                            # 查找对应的 ProductPreset（Gap_Extender）
                            for pp in project.product_presets:
                                if pp.name == ext_name and pp.product_type == "Gap_Extender":
                                    ext_product = _make_gap_extender_product(
                                        result.parts, pp, project,
                                    )
                                    # 填充 Gap Extender 对应关系表元数据
                                    ext_product.product_type = "Gap_Extender"
                                    ext_product.inv_name = inv.name
                                    ext_product.node_name = node.name
                                    ext_product.row_name = row.name if row.name else f"Row{row_num}"
                                    ext_product.rackings_name = entry.rackings
                                    ext_product.preset_name = pp.name
                                    ext_product.preset_remarks = pp.remarks
                                    result.all_products.append(ext_product)
                                    _add_gap_extender_product_with_dedup(
                                        result.gap_extender_products,
                                        ext_product, pp, ext_qty,
                                    )
                                    break

        # ---- 命名阶段：为 semi / ibex / lynx / gap extender 分配编号 ----
        # semi: 按产品类型和极性分配前缀
        #   IBEX 外层（带 T 转接件）: TP1, TN1, ...
        #   IBEX 内层（不带 T）     : BP1, BN1, ...
        #   LYNX 外层（带 overmold）: OP1, ON1, ...
        #   LYNX 内层（不带 overmold）: EP1, EN1, ...
        for sp in result.semi_parts:
            if not sp.code:
                prefix = _detect_semi_prefix(sp)
                sp.code = _get_next_semi_code_by_prefix(result.semi_parts, prefix)

        # ibex: HA, HB, ..., HZ, HAA, HAB, ...
        for prod in result.ibex_products:
            if not prod.code:
                prod.code = _get_next_alpha_code(result.ibex_products, "H")

        # lynx: LA, LB, ..., LZ, LAA, LAB, ...
        for prod in result.lynx_products:
            if not prod.code:
                prod.code = _get_next_alpha_code(result.lynx_products, "L")

        # gap extender: 使用 preset 名称作为 product code
        for prod in result.gap_extender_products:
            if not prod.code:
                ext_preset = getattr(prod, "_gap_extender_preset", None)
                if ext_preset is not None:
                    prod.code = ext_preset.name

        # homerun extender: EA, EB, ..., EZ, EAA, EAB, ...
        # 仅目标主线长度不同的变体共用同一个类别编号。
        for product_class in cluster_homerun_extender_products(
            result.homerun_extender_products
        ):
            class_code = next(
                (prod.code for prod in product_class.products if prod.code),
                "",
            )
            if not class_code:
                class_code = _get_next_alpha_code(
                    result.homerun_extender_products,
                    "E",
                )
            for prod in product_class.products:
                prod.code = class_code

        # ---- 为 all_products 中因去重而未命名的产品补充分配编号 ----
        # 去重后的产品已在命名阶段获得编号；被去重的产品应使用与其匹配的
        # 去重后产品的编号，而非重新分配新编号。
        for prod in result.all_products:
            if prod.code:
                continue
            if prod.product_type == "IBEX":
                for existing in result.ibex_products:
                    if _assembled_products_equal(existing, prod):
                        prod.code = existing.code
                        break
            elif prod.product_type == "LYNX":
                for existing in result.lynx_products:
                    if _assembled_products_equal(existing, prod):
                        prod.code = existing.code
                        break
            elif prod.product_type == "Homerun_Extender":
                for existing in result.homerun_extender_products:
                    if _assembled_products_equal(existing, prod):
                        prod.code = existing.code
                        break
            elif prod.product_type == "Gap_Extender":
                # Gap Extender 使用 preset 名称作为 code
                for existing in result.gap_extender_products:
                    if existing.code:
                        existing_preset = getattr(existing, "_gap_extender_preset", None)
                        prod_preset = getattr(prod, "_gap_extender_preset", None)
                        if existing_preset is not None and prod_preset is not None:
                            from .gap_extender import _gap_extender_presets_equal
                            if _gap_extender_presets_equal(existing_preset, prod_preset):
                                prod.code = existing.code
                                break
                if not prod.code:
                    ext_preset = getattr(prod, "_gap_extender_preset", None)
                    if ext_preset is not None:
                        prod.code = ext_preset.name
                    elif prod.preset_name:
                        prod.code = prod.preset_name

        return result
