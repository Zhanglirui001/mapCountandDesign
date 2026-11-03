# Count and Design — 数据类型参考文档

---

## 目录

- [1. 项目概览](#1-项目概览)
- [2. 领域模型 (Domain Models)](#2-领域模型-domain-models)
  - [2.1 Project](#21-project)
  - [2.2 LengthIdentifier](#22-lengthidentifier)
  - [2.3 Length](#23-length)
  - [2.4 Wire](#24-wire)
  - [2.5 Connector](#25-connector)
  - [2.6 Fuse](#26-fuse)
  - [2.7 Inv](#27-inv)
  - [2.8 Node](#28-node)
  - [2.9 Row / RackingEntry](#29-row--rackingentry)
  - [2.10 Rackings](#210-rackings)
  - [2.11 Part](#211-part)
  - [2.12 AssembledProduct](#212-assembledproduct)
  - [2.13 IBEXBranchCoordinate](#213-ibexbranchcoordinate)
  - [2.14 IBEXBranch](#214-ibexbranch)
  - [2.15 IBEXProduct](#215-ibexproduct)
  - [2.16 LYNXBranch](#216-lynxbranch)
  - [2.17 Overmold](#217-overmold)
  - [2.18 LYNXProduct](#218-lynxproduct)
  - [2.19 ProductPreset](#219-productpreset)
  - [2.20 DataFillTarget / DataFillTerm / QuantityRef](#220-datafilltarget--datafillterm--quantityref)
- [3. 视图状态模型 (View State Models)](#3-视图状态模型-view-state-models)
  - [3.1 ListItemState](#31-listitemstate)
  - [3.2 AppState](#32-appstate)
  - [3.3 CountTabState](#33-counttabstate)
  - [3.4 TreeNodeState](#34-treenodestate)
  - [3.5 EditorState](#35-editorstate)
  - [3.6 TableSectionState](#36-tablesectionstate)
  - [3.7 RackingsTabState / RackingsDetailState](#37-rackingstabstate--rackingsdetailstate)
  - [3.8 ProductPresetsTabState](#38-productpresetstabstate)
  - [3.9 ProductPresetDetailState](#39-productpresetdetailstate)
  - [3.10 LengthIdentifiersTabState](#310-lengthidentifierstabstate)
- [4. 服务层辅助类型](#4-服务层辅助类型)
  - [4.1 ParentContext](#41-parentcontext)
  - [4.2 RackingsRenameResult](#42-rackingsrenameresult)
  - [4.3 ProductGenerationResult](#43-productgenerationresult)
- [5. 类型关系图](#5-类型关系图)

---

## 1. 项目概览

该项目采用分层架构：

```
┌─────────────────────────────┐
│  GUI Layer                  │
│  ┌───────────────────────┐  │
│  │ Views (Qt widgets)    │  │
│  │ ViewModels (states)   │  │
│  └───────────────────────┘  │
├─────────────────────────────┤
│  Application Layer          │
│  ┌───────────────────────┐  │
│  │ Services              │  │
│  └───────────────────────┘  │
├─────────────────────────────┤
│  Domain Layer               │
│  ┌───────────────────────┐  │
│  │ Models (dataclasses)  │  │
│  └───────────────────────┘  │
└─────────────────────────────┘
```

所有领域模型均使用 `@dataclass` 实现，位于 `src/count_and_design/models/` 目录下。视图状态模型同样使用 `@dataclass(frozen=True)` 实现（不可变，用于 Qt 单向数据流），位于 `src/count_and_design/gui/viewmodels/states.py`。

---

## 2. 领域模型 (Domain Models)

### 2.1 Project

**文件**: `models/project.py`

项目的顶级容器，代表一个完整的工程文件。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | `str` | `""` | 项目名称 |
| `address` | `str` | `""` | 项目地址 |
| `client_name` | `str` | `""` | 客户名称 |
| `project_type` | `str` | `""` | 项目类型，有效值: `"IBEX"` 或 `"LYNX"`（空字符串表示未设置） |
| `length_identifiers` | `list[LengthIdentifier]` | `[]` | 长度标识符列表，用于在 Length 表达式中引用 |
| `rackings_list` | `list[Rackings]` | `[]` | 支架组合定义列表 |
| `product_presets` | `list[ProductPreset]` | `[]` | 产品预设列表（IBEX 类型：名称是单个大写字母；LYNX 类型：名称格式为 "L" + 大写字母；Gap_Extender 类型：名称不能为空） |
| `racking_pitch` | `Length \| None` | `None` | 全局支架间距（默认值） |
| `cluster_wire` | `Wire \| None` | `None` | 全局 LYNX Cluster 延长线线径（默认值） |
| `lynx_preset` | `str \| None` | `None` | 全局 LYNX 产品预设引用（名称，优先级最低，可被 Inv/Node 覆盖） |
| `invs` | `list[Inv]` | `[]` | 逆变器列表 |
| `data_fill_targets` | `list[DataFillTarget]` | `[]` | 按优先级保存的凑数据公式目标 |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `__post_init__()` | 校验 `project_type`，并调用 `_bind_length_identifiers()` 将标识符绑定到所有 Length 实例 |
| `to_dict()` → `dict` | 将项目序列化为字典 |
| `from_dict(data)` → `Project` | 从字典反序列化 |
| `to_json(path)` | 原子写入项目 JSON。文件包含 `schema_version`（当前为 2）、`app_version` 和 `file_integrity` |
| `from_json(path)` → `Project` | 从 JSON 文件加载项目。支持 schema 1/2；schema 2 会验证 SHA-256 内容哈希 |
| `rebind_length_identifiers()` | 重新绑定长度标识符（在修改标识符列表后调用） |
| `get_rackings(name)` → `Rackings \| None` | 按名称查找支架组合 |
| `swap(index1, index2)` | 交换两个 INV 的位置 |
| `would_create_length_identifier_cycle(changed_name, new_length_str)` → `bool` | 检测设置某个标识符为指定表达式时是否会引入循环依赖 |
| `replace_length_identifier_references(old_name, new_name)` | 将项目中所有 Length 表达式中对 `old_name` 的引用替换为 `new_name`（基于 token 级别替换，避免误替换子串） |

**约束**:
- `project_type` 非空时必须为 `"IBEX"` 或 `"LYNX"`

**文件完整性与历史格式**:

- `file_integrity` 使用 SHA-256 校验规范化 JSON 内容，覆盖项目字段和 `version_tree`，但不受缩进、换行或键顺序影响。
- schema 1 无哈希文件可以加载，但已有历史节点只读；当前打开状态是新历史的导航边界。
- schema 2 缺少哈希或哈希不匹配时，项目数据仍可打开，但不可信的旧历史会被丢弃。
- 历史格式 v2 使用带类型的 `add` / `replace` / `remove` 补丁；列表身份或顺序变化使用原子列表替换。
- 构造后自动将 `length_identifiers` 绑定到所有嵌套的 `Length` 实例

---

### 2.2 LengthIdentifier

**文件**: `models/length_identifier.py`

长度标识符，允许在 Length 表达式中使用字母/下划线组成的变量名作为引用。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | `str` | *(必填)* | 标识符名称，必须匹配正则 `^[a-zA-Z_]+$`（一个或多个字母或下划线） |
| `length` | `Length` | *(必填)* | 该标识符对应的长度值 |
| `label` | `str` | `""` | 备注标签 |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `__post_init__()` | 校验 `name` 匹配 `^[a-zA-Z_]+$`；校验名称不与保留单位名（mm, ft, inch, in, m）冲突 |
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data)` → `LengthIdentifier` | 反序列化 |

**约束**:
- `name` 必须是字母/下划线组成的字符串，且不能是保留单位名（不区分大小写），否则抛出 `LengthError`

---

### 2.3 Length

**文件**: `models/length.py`

长度值，支持多种单位、算术表达式和标识符引用。内部统一以毫米 (`mm`) 存储。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `length_str` | `str` | `"0mm"` | 用户输入的长度表达式字符串 |
| `identifiers` | `list[LengthIdentifier]` | `[]` | 用于解析表达式中标识符变量的列表（repr 中隐藏） |
| `_length_mm` (init=False) | `float` | — | 内部存储的毫米值（不在 `__init__` 中显式传入） |
| `_needs_identifier_resolution` (init=False) | `bool` | `False` | 标记是否需要标识符解析 |

**属性**:

| 属性 | 类型 | 说明 |
|------|------|------|
| `length_mm` | `float` (可读写) | 以毫米为单位的值。setter 会同步更新 `length_str` |

**支持的表达式语法**（四层递归下降解析器）:

| 元素 | 示例 | 说明 |
|------|------|------|
| 数字 | `10`, `3.5` | 浮点数值 |
| 单位 | `mm`, `m`, `ft`, `inch`/`in` | 紧跟在数字后，如 `10ft` |
| 标识符 | `A`, `S`, `id_X` | 字母/下划线变量，引用 `LengthIdentifier` |
| 运算符 | `+`, `-`, `*`, `/` | 四则运算 |
| 括号 | `(`, `)` | 分组 |
| 隐式加法 | `5S3R` | 形如 `5S3R` 的连续原子自动展开为 `5*S + 3*R` |
| 一元负号 | `-3ft` | 负值 |
| 数字+标识符 | `5S` | 数字后紧跟标识符表示乘法：`5 * S` |

**表达式示例**:
- `10mm` — 10 毫米
- `4ft+2inch` — 4 英尺 + 2 英寸
- `A+1m` — 标识符 A 的值 + 1 米
- `(B-2ft)*2+3mm` — 括号分组与乘除
- `5S3R` — 隐式展开为 `5*S + 3*R`

**关键方法**:

| 方法 | 说明 |
|------|------|
| `set_identifiers(ids)` | 设置标识符列表并重新计算 |
| `set_length_str(value)` | 设置表达式字符串并重新计算 |
| `_recalculate()` | 内部重新计算；若表达式含未解析的标识符则标记为需要解析 |
| `extract_identifier_refs(length_str)` → `set[str]` | 静态方法，提取表达式中的所有标识符引用 |
| `to_mm()` → `float` | 转换为毫米（访问时若表达式含未解析的标识符，会自动尝试延迟重算） |
| `to_m()` → `float` | 转换为米（内部调用 `to_mm()`，同样触发延迟重算） |
| `to_ft()` → `float` | 转换为英尺 |
| `to_inch()` → `float` | 转换为英寸 |
| `to_dict()` → `dict` | 序列化（包含 `length_str` 和 `length_mm`） |
| `from_dict(data, identifiers)` → `Length \| None` | 反序列化。若旧数据仅有 `length_mm` 而无 `length_str`，则从毫米值反向生成表达式 |
| `__add__(other)` | 加法运算符重载 |
| `__sub__(other)` | 减法运算符重载 |
| `__mul__(scalar)` | 乘法运算符重载 |
| `__rmul__(scalar)` | 右乘运算符重载 |
| `__truediv__(scalar)` | 除法运算符重载 |
| `__eq__` / `__ne__` / `__lt__` / `__le__` / `__gt__` / `__ge__` | 比较运算符重载 |

**单位换算**:
- 1 m = 1000 mm
- 1 ft = 304.8 mm
- 1 inch (in) = 25.4 mm

**约束**:
- 表达式不能为空
- 所有 token 必须能被正则 `[a-zA-Z_]+|\d+(?:\.\d+)?|[+\-*/()]` 识别
- 标识符必须在 `identifiers` 中有对应项
- 表达式不能以运算符结尾
- 表达式必须求值为长度维度（dim=1），不能为标量或面积等

---

### 2.4 Wire

**文件**: `models/wire.py`

导线规格，支持美制 (AWG/MCM) 和公制 (mm²) 两种线径表示，铜/铝两种材料，以及股数参数。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `wiresize` | `str` | *(必填)* | 线径规格，支持 AWG/MCM、公制，以及 `"none"`（表示无线径） |
| `material` | `str` | *(必填)* | 材料，有效值: `"Cu"`（铜）或 `"Al"`（铝） |
| `strands` | `str` | `"none"` | 股数，有效值: `"none"`, `"7"`, `"48"`, `"66"`, `"75"`, `"106"` |

**股数默认值**（当线径为具体值时自动推荐，通过 `CU_STRANDS_DEFAULTS` 和 `AL_STRANDS_DEFAULT` 定义）:

| 条件 | 默认股数 |
|------|----------|
| 铜线 12AWG | `"48"` |
| 铜线 10AWG | `"75"` |
| 铜线 6AWG | `"7"` |
| 铝线（任意具体线径） | `"7"` |

**模块级常量**:

| 常量 | 类型 | 说明 |
|------|------|------|
| `WIRE_SIZE_MAP` | `dict[str, str]` | AWG/MCM → 公制对照表 |
| `REVERSE_WIRE_SIZE_MAP` | `dict[str, str]` | 公制 → AWG/MCM 对照表（由 `WIRE_SIZE_MAP` 反转生成） |
| `VALID_MATERIALS` | `set[str]` | 有效材料集合：`{"Cu", "Al"}` |
| `VALID_STRANDS` | `set[str]` | 有效股数集合：`{"none", "7", "48", "66", "75", "106"}` |
| `CU_STRANDS_DEFAULTS` | `dict[str, str]` | 铜线股数默认值：`12AWG→"48"`, `10AWG→"75"`, `6AWG→"7"` |
| `AL_STRANDS_DEFAULT` | `str` | 铝线股数默认值：`"7"` |

**线径对照表**:

| AWG/MCM | 公制 (mm²) |
|---------|-----------|
| `none` | `none` |
| `14AWG` | `2.5mm2` |
| `12AWG` | `4mm2` |
| `10AWG` | `6mm2` |
| `8AWG` | `10mm2` |
| `6AWG` | `16mm2` |
| `4AWG` | `25mm2` |
| `2AWG` | `35mm2` |
| `1/0AWG` | `50mm2` |
| `2/0AWG` | `70mm2` |
| `3/0AWG` | `95mm2` |
| `4/0AWG` | `120mm2` |
| `300MCM` | `150mm2` |
| `350MCM` | `185mm2` |
| `400MCM` | `200mm2` |
| `500MCM` | `240mm2` |
| `600MCM` | `300mm2` |
| `750MCM` | `400mm2` |
| `900MCM` | `500mm2` |
| `1000MCM` | `630mm2` |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `__post_init__()` | 规范化并校验线径（大小写不敏感）；校验材料和股数 |
| `_normalize_size(value)` → `str` | 大小写不敏感地检查线径是否在对照表中 |
| `to_metric()` → `str` | 转换为公制表示 |
| `to_awg()` → `str` | 转换为 AWG/MCM 表示 |
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data)` → `Wire \| None` | 反序列化。data 为 `None` 时返回 `None` |

**约束**:
- `wiresize` 必须在对照表中（或为 `"none"`），大小写不敏感（如 `"12awg"` 会规范化为 `"12AWG"`），否则抛出 `ValueError`
- 当 `wiresize` 为 `"none"` 时，`to_metric()` 和 `to_awg()` 均返回 `"none"`
- `material` 必须为 `"Cu"` 或 `"Al"`
- `strands` 必须在 `{"none", "7", "48", "66", "75", "106"}` 中

---

### 2.5 Connector

**文件**: `models/connector.py`

连接器规格，包含线号、类型和制造商信息。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `size` | `Wire` | *(必填)* | 连接器适用的线号 |
| `type` | `str` | *(必填)* | 连接器类型 |
| `manufacturer` | `str` | *(必填)* | 制造商 |

**有效类型 (`type`)**:

```
MC4, S418, MC4-EVO2A, T6, QC4.10,
PV-ZH202B, JK03M2, TS4 PLUS, PV-JM608, PV4-S, RY01,
XT101.1, PV-LR5, TT02, T6-2C, PV-SY02, TS4,
TS4-PLUS-3, PV-RY01, H4-Plus, PV-TS02, PV-RY02,
PV5, Z4S, none
```

**有效制造商 (`manufacturer`)**:

```
Staubli, Bizlink, Tlian, QC Solar, Zhonghuan,
Jinko, Trina, Jiaming, TE connectivity, Runergy,
Xietong, Longi, Chuangda, Risen, Amphenol, Tiansheng,
Zerun, none
```

**关键方法**:

| 方法 | 说明 |
|------|------|
| `__post_init__()` | 校验 `type`，并根据类型自动设置对应制造商 |
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data)` → `Connector \| None` | 反序列化 |

**约束**:
- `type` 必须在有效类型集合中，否则抛出 `ValueError`
- `manufacturer` 由 `type` 自动确定，传入值不一致时会被对应制造商覆盖

---

### 2.6 Fuse

**文件**: `models/fuse.py`

保险丝规格，包含电流额定值和制造商信息。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `current` | `str` | *(必填)* | 电流额定值 |
| `manufacturer` | `str` | *(必填)* | 制造商 |

**有效电流 (`current`)**:

```
"4A", "5A", "10A", "15A", "20A", "25A", "30A", "32A",
    "35A", "40A", "45A", "50A", "55A", "60A", "65A", "80A", "none"
```

**有效制造商 (`manufacturer`)**:

```
Adler, none
```

**关键方法**:

| 方法 | 说明 |
|------|------|
| `__post_init__()` | 校验 `current` 和 `manufacturer` 是否在有效值集合中 |
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data)` → `Fuse \| None` | 反序列化 |

**约束**:
- `current` 必须在有效电流集合中，否则抛出 `ValueError`
- `manufacturer` 必须在有效制造商集合中，否则抛出 `ValueError`

---

### 2.7 Inv

**文件**: `models/inv.py`

逆变器 (Inverter)，位于 Project 下，包含若干 Node。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | `str` | *(必填)* | 逆变器名称（如 `"INV1"`, `"INV2"`） |
| `racking_pitch` | `Length \| None` | `None` | 该 INV 范围内的支架间距（优先级高于 Project 的设置） |
| `cluster_wire` | `Wire \| None` | `None` | 该 INV 范围内的 LYNX Cluster 延长线线径（优先级高于 Project 的设置，但低于 LYNX preset 的配置） |
| `lynx_preset` | `str \| None` | `None` | 该 INV 范围内引用的 LYNX 产品预设名称（优先级介于 Project 全局和 Node 局部之间，为空时继承上层设置） |
| `nodes` | `list[Node]` | `[]` | 节点列表 |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `sequence_name(previous_name)` → `str` | 根据前一个名称生成序列号递增的名称 |
| `new(previous_name)` → `Inv` | 创建新的 Inv 实例（自动命名） |
| `clone()` → `Inv` | 深拷贝（用于复制粘贴） |
| `swap(index1, index2)` | 交换两个 Node 的位置 |
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data, identifiers)` → `Inv` | 反序列化 |

**命名规则**:
- 从 `previous_name` 中提取末尾数字，加 1 后补零对齐
- 首个默认名称为 `"INV1"`

---

### 2.8 Node

**文件**: `models/node.py`

节点，位于 Inv 下，包含若干 Row。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | `str` | *(必填)* | 节点名称（如 `"Node1"`, `"Node2"`） |
| `racking_pitch` | `Length \| None` | `None` | 该 Node 范围内的支架间距（优先级最高） |
| `cluster_wire` | `Wire \| None` | `None` | 该 Node 范围内的 LYNX Cluster 延长线线径（优先级最高，但低于 LYNX preset 的配置） |
| `lynx_preset` | `str \| None` | `None` | 该 Node 范围内引用的 LYNX 产品预设名称（优先级最高，为空时继承 Inv 或 Project 的设置） |
| `cluster_length` | `Length \| None` | `None` | LYNX Cluster 延长长度 |
| `box_position` | `float` | `0.0` | 箱子位置 |
| `rows` | `list[Row]` | `[]` | 排列表 |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `sequence_name(previous_name)` → `str` | 生成序列号名称 |
| `new(previous_name)` → `Node` | 创建新 Node（自动命名） |
| `clone()` → `Node` | 深拷贝 |
| `swap(index1, index2)` | 交换两个 Row 的位置 |
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data, identifiers)` → `Node` | 反序列化 |

**命名规则**: 与 Inv 类似，首项为 `"Node1"`。

**优先级说明**: `racking_pitch`、`cluster_wire` 和 `lynx_preset` 遵循优先级链：
```
Node 局部设置 > Inv 设置 > Project 全局设置
```
LYNX 产品生成时使用的 `cluster_wire` 由选定的 LYNX preset 提供，优先级链为：
```
Node.lynx_preset > Inv.lynx_preset > Project.lynx_preset
```

---

### 2.9 Row / RackingEntry

**文件**: `models/row.py`

排 (Row)，位于 Node 下，由多个 RackingEntry 组成。Row 中包含一个 `racking_entries` 列表，每项是一个 `RackingEntry`（支架名称 + 偏移量）对。

#### RackingEntry

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `rackings` | `str` | `""` | 支架组合的名称（引用 `Rackings.name`） |
| `offset` | `Length` | `Length("0mm")` | 该支架组合偏离通常位置的距离 |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data, identifiers)` → `RackingEntry` | 反序列化（自动兼容旧 dict 格式的 `rackings`） |
| `clone()` → `RackingEntry` | 深拷贝 |

#### Row

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | `str` | `""` | 排名称（如 `"Row1"`） |
| `racking_entries` | `list[RackingEntry]` | `[RackingEntry()]` | 支架组合条目列表（至少包含一个默认条目） |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `sequence_name(previous_name)` → `str` | 生成序列号名称 |
| `new(previous_name)` → `Row` | 创建新 Row（自动命名） |
| `clone()` → `Row` | 深拷贝 |
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data, identifiers)` → `Row` | 反序列化（兼容旧格式：若数据中无 `racking_entries`，则从旧的 `rackings_NW`/`rackings_SE`/`offset_NW`/`offset_SE` 字段自动迁移） |

**命名规则**: 首项为 `"Row1"`。

**兼容性**: `from_dict` 支持旧数据格式——当不存在 `racking_entries` 字段时，会从旧的 `rackings_NW` / `rackings_SE` / `offset_NW` / `offset_SE` 字段自动迁移生成 `RackingEntry` 列表。旧格式中的 dict 类型 `rackings` 也会自动提取 `name` 字段。

---

### 2.10 Rackings

**文件**: `models/rackings.py`

支架组合定义，表示一组产品的集合，可包含 IBEX 产品和 Gap Extender。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | `str` | *(必填)* | 支架组合名称 |
| `product_presets` | `str` | `""` | IBEX 产品名称字符串，由若干个连续大写字母组成（如 `"ABC"`） |
| `gap_extenders` | `list[tuple[str, int]]` | `[]` | Gap Extender 列表，每项为 `(名称, 数量)` |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `__post_init__()` | 校验 `product_presets` 必须全部为大写字母 |
| `product_preset_names()` → `list[str]` | 将 `product_presets` 字符串拆分为单个字符列表 |
| `gap_extender_names()` → `list[str]` | 返回所有 Gap Extender 的名称列表 |
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data)` → `Rackings \| None` | 反序列化（兼容旧 `extenders` 字段名，兼容 list-of-list 和 list-of-dict 格式） |

**约束**:
- `product_presets` 非空时，必须匹配 `^[A-Z]+$`

**示例**:
- `Rackings(name="TypeA", product_presets="ABC", gap_extenders=[("GA", 2)])` → 表示该支架组合包含 IBEX 产品 A、B、C 以及 2 个 Gap Extender GA

---

### 2.11 Part

**文件**: `models/part.py`

部件/半成品，表示产品生成过程中产生的单个零件。支持递归嵌套半成品子部件。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `code` | `str` | `""` | 部件编码 |
| `category` | `str` | `""` | 部件类别，如 `"wire"`, `"connect"`, `"Fuse"`, `"AC"`, `"OverMolding"`, `"Label"`, `"Kit"` 等 |
| `cad_parts_no` | `str` | `""` | CAD 零件编号 |
| `cad_parts_length` | `float` | `0.0` | CAD 零件长度 (mm) |
| `description` | `str` | `""` | 描述文本 |
| `unit` | `str` | `""` | 单位（如 `"pcs"`, `"ft"`） |
| `overmolding_detail` | `str` | `""` | 注塑件明细 |
| `label_detail` | `str` | `""` | 标签明细 |
| `direction_nos` | `list[int]` | `[]` | 方向编号数组，按 semi_list 中 semi_or_not=True 的子部件顺序排列。direction 表示子部件在父部件中的安装位置，属于父-子关系属性而非子部件自身属性，故存储在父级而非写入子部件 |
| `length` | `float` | `0.0` | 实际长度 (ft) |
| `semi_or_not` | `bool` | `False` | 是否为半成品 |
| `semi_list` | `list[Part]` | `[]` | 半成品子部件列表（递归结构） |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data)` → `Part` | 反序列化 |

---

### 2.12 AssembledProduct

**文件**: `models/assembled_product.py`

组装后的产品（正极或负极），包含部件列表。产品生成流程的最终输出之一。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `code` | `str` | `""` | 产品编码 |
| `polarity` | `str` | `""` | 极性，有效值: `"POS"` 或 `"NEG"` |
| `quantity` | `int` | `1` | 数量 |
| `description` | `str` | `""` | 描述文本 |
| `pv_panel` | `str` | `""` | 光伏面板名称 |
| `parts` | `list[Part]` | `[]` | 组成该产品的部件列表 |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data)` → `AssembledProduct` | 反序列化 |

---

### 2.13 IBEXBranchCoordinate

**文件**: `models/ibex.py`

IBEX 支线坐标，以第一个铜铝转接器为参考起点，定义正负方向上的坐标。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `index` | `int` | `1` | 支线序号 |
| `coordinate_positive` | `Length` | `Length("0mm")` | 支线坐标（正方向），从第一个铜铝转接器起算 |
| `coordinate_negative` | `Length` | `Length("0mm")` | 支线坐标（负方向），从第一个铜铝转接器起算 |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `__post_init__()` | 确保 coordinate 字段为 `Length` 实例 |
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data, identifiers)` → `IBEXBranchCoordinate` | 反序列化 |

---

### 2.14 IBEXBranch

**文件**: `models/ibex.py`

IBEX 支线，表示一条支线及其前置长度。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `index` | `int` | `0` | 支线序号 |
| `prefix_length` | `Length` | `Length("0mm")` | 前置长度 |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `__post_init__()` | 确保 `prefix_length` 为 `Length` 实例 |
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data, identifiers)` → `IBEXBranch` | 反序列化 |

---

### 2.15 IBEXProduct

**文件**: `models/ibex.py`

IBEX 产品实例，引用一个 ProductPreset 并补充具体参数。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `product_preset` | `str` | *(必填)* | 引用的产品预设名称 |
| `product_code` | `str` | `""` | 产品代号 |
| `polarity` | `str` | `"POS"` | 产品极性，有效值: `"POS"` 或 `"NEG"` |
| `extender_length` | `Length` | `Length("0mm")` | Extender 长度 |
| `whip_length` | `Length` | `Length("0mm")` | Whip 长度 |
| `branches` | `list[IBEXBranch]` | `[]` | 支线列表 |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `__post_init__()` | 校验 `polarity` 必须为 POS 或 NEG；确保 Length 字段类型正确 |
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data, identifiers)` → `IBEXProduct` | 反序列化（兼容旧字段名 `extender_length`） |

**约束**:
- `polarity` 必须为 `"POS"` 或 `"NEG"`，否则抛出 `ValueError`

---

### 2.16 LYNXBranch

**文件**: `models/lynx.py`

LYNX 支线，表示注塑件中的一条支线的线径和长度。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `location` | `int` | *(必填)* | LYNX 支线位置 |
| `cu_wire` | `Wire` | *(必填)* | 支线铜线线号 |
| `al_wire` | `Wire` | *(必填)* | 支线铝线线号 |
| `cu_length` | `Length` | `Length("11ft")` | 支线铜线长度 |
| `al_length` | `Length` | `Length("1ft")` | 支线铝线长度 |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `__post_init__()` | 确保 `cu_length` 和 `al_length` 为 `Length` 实例 |
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data, identifiers)` → `LYNXBranch` | 反序列化 |

---

### 2.17 Overmold

**文件**: `models/lynx.py`

LYNX 注塑件，包含多条 LYNX 支线和一个前置长度。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `branch_lines` | `list[LYNXBranch]` | `[]` | LYNX 支线对象列表 |
| `prefix_length` | `Length` | `Length("0mm")` | 前置长度 |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `__post_init__()` | 确保 `prefix_length` 为 `Length` 实例 |
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data, identifiers)` → `Overmold` | 反序列化 |

---

### 2.18 LYNXProduct

**文件**: `models/lynx.py`

LYNX 产品实例，包含产品代号、主线线号和注塑件列表。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `product_code` | `str` | *(必填)* | 产品代号 |
| `mainline_wire` | `Wire` | *(必填)* | 主线线号 |
| `overmolds` | `list[Overmold]` | `[]` | 注塑件列表 |

**关键方法**:

| 方法 | 说明 |
|------|------|
| `to_dict()` → `dict` | 序列化 |
| `from_dict(data, identifiers)` → `LYNXProduct` | 反序列化 |

---

### 2.19 ProductPreset

**文件**: `models/product_preset.py`

产品预设模板，包含 IBEX、Homerun Extender、Placeholder、LYNX 或 Gap Extender 类型的公共参数与特有参数。通过 `product_type` 字段区分类型。

每个 `ProductPreset` 实例通过 `name` 挂载在 `Project.product_presets` 下。IBEX、Homerun Extender 和 Placeholder 由 `Rackings.product_presets` 字符串中的字母引用；LYNX 类型由 `Project`、`Inv`、`Node` 的 `lynx_preset` 字段引用；Gap Extender 由 `Rackings.gap_extenders` 列表引用。

#### 公共字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | `str` | *(必填)* | 产品预设名称：IBEX/Homerun/Placeholder 必须是单个大写字母 (A-Z)；LYNX 类型必须以 "L" 开头后跟大写字母（如 "LA"）；Gap_Extender 类型不能为空 |
| `product_type` | `str` | `"IBEX"`（随后按 `branch_count` 规范化） | 产品类型，有效值: `"IBEX"`, `"Homerun_Extender"`, `"Placeholder"`, `"LYNX"`, `"Gap_Extender"` |
| `remarks` | `str` | `""` | 备注 |
| `pv_panel` | `str` | `""` | 光伏面板名称 |
| `branch_cu_wire` | `Wire` | `Wire("none", "Cu")` | 支线铜线号（IBEX/LYNX 共用） |
| `branch_al_wire` | `Wire` | `Wire("none", "Al")` | 支线铝线号（IBEX/LYNX 共用） |

#### IBEX 特有字段

以下字段均有 "none" 默认值，因此仅用 `name` 即可创建 ProductPreset（后续可通过 UI 完善）：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `branch_count` | `int` | `1` | 支线数量；0/1/>1 自动对应 Placeholder/Homerun_Extender/IBEX |
| `branch_lines` | `list[IBEXBranchCoordinate]` | `[]` | 支线坐标列表 |
| `product_total_length` | `Length` | `Length("0mm")` | 跨该产品所需长度 |
| `branch_connector` | `Connector` | `Connector(none, none, none)` | 支线连接器 |
| `branch_fuse` | `Fuse` | `Fuse(none, none)` | 支线保险丝 |
| `last_branch_cu_wire` | `Wire` | `Wire("none", "Cu")` | 末端支线（最后一条支线）铜线号；未配置（"none"）时回退到 `branch_cu_wire`（见 `effective_last_branch_cu_wire`） |
| `last_branch_al_wire` | `Wire` | `Wire("none", "Al")` | 末端支线（最后一条支线）铝线号；未配置（"none"）时回退到 `branch_al_wire`（见 `effective_last_branch_al_wire`） |
| `last_branch_fuse` | `Fuse` | `Fuse(none, none)` | 末端支线（最后一条支线）保险丝；未配置（"none"）时回退到 `branch_fuse`（见 `effective_last_branch_fuse`） |
| `external_cu_wire` | `Wire` | `Wire("none", "Cu")` | 外部主线铜线号 |
| `external_al_wire` | `Wire` | `Wire("none", "Al")` | 外部主线铝线号 |
| `external_fuse` | `Fuse` | `Fuse(none, none)` | 外部主线保险丝 |
| `external_connector` | `Connector` | `Connector(none, none, none)` | 外部主线连接器 |
| `internal_cu_wire` | `Wire` | `Wire("none", "Cu")` | 内部主线铜线号 |
| `internal_al_wire` | `Wire` | `Wire("none", "Al")` | 内部主线铝线号 |

Placeholder 不生成产品、single line section 或压降段，只将自己的
`product_total_length` 累加到同一 Rackings 中排在其后的产品。

#### IBEX 默认参数

这些字段有预设默认值，可以在创建时覆盖：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `branch_length_positive` | `Length` | `Length("4ft")` | 支线长度（正） |
| `branch_length_negative` | `Length` | `Length("4ft")` | 支线长度（负） |
| `last_branch_with_extra_length` | `bool` | `False` | 最后一条支线是否额外加支线长度 |
| `branch_fuse_prefix` | `Length` | `Length("1ft")` | 支线保险丝离前一个部件的距离 |
| `branch_first_cual_prefix` | `Length` | `Length("2ft")` | 末端支线铜铝头到前一个 T-mold 间距 |
| `branch_second_cual_suffix` | `Length` | `Length("2ft")` | 末端支线铜铝头到连接器间距 |
| `external_fuse_suffix` | `Length` | `Length("1ft")` | 外部主线保险丝离排头的距离 |
| `external_cu_length_to_row_head` | `Length` | `Length("3ft")` | 外部主线延长线到排头距离 |
| `external_second_cual_suffix` | `Length` | `Length("1ft")` | 外部主线第二个铜铝转接器离后方 T 转接件的距离 |
| `external_extension_trench_length` | `Length` | `Length("0mm")` | 外部主线穿管长度 |
| `internal_first_cual_prefix` | `Length` | `Length("1ft")` | 内部主线第一个铜铝转接器离前方 T 转接件距离 |
| `internal_second_cual_suffix` | `Length` | `Length("1ft")` | 内部主线第二个铜铝转接器离后方 T 转接件距离 |
| `external_al_threshold` | `Length` | `Length("100ft")` | 外部主线铜铝转接阈值：跨度 ≥ 该值时外部主线采用铜铝转接方案 |
| `last_branch_al_threshold` | `Length` | `Length("100ft")` | 末端支线铜铝转接阈值：跨度 ≥ 该值时末端支线采用铜铝转接方案 |
| `internal_al_threshold` | `Length` | `Length("100ft")` | 内部主线（相邻支线之间的跨接线，仅 `branch_count >= 3` 时生成）铜铝转接阈值：相邻支线坐标跨度 ≥ 该值时内部主线采用铜铝转接方案；旧存档缺少该键时回退到该预设的 `external_al_threshold` |

#### LYNX 特有字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `branch_length_cu` | `Length` | `Length("11ft")` | 支线铜长度 |
| `branch_length_al` | `Length` | `Length("1ft")` | 支线铝长度 |
| `cluster_wire` | `Wire` | `Wire("none", "Al")` | LYNX Cluster 延长线线号（由 ProductPreset 提供，通过 project/inv/node 的 lynx_preset 引用） |

#### Gap Extender 特有字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `connector_male` | `Connector` | `Connector(none, none, none)` | 公头连接器 |
| `connector_female` | `Connector` | `Connector(none, none, none)` | 母头连接器 |
| `cu_wire` | `Wire` | `Wire("none", "Cu")` | 铜线线号 |
| `length` | `Length` | `Length("0mm")` | Gap Extender 长度 |
| `label` | `str` | `""` | 标签 |

**连接器线号自动同步**:
- IBEX: `branch_connector.size` 自动跟随 `branch_cu_wire`；`external_connector.size` 自动跟随 `external_cu_wire`
- Homerun Extender：生成支线连接器时使用 `external_cu_wire` 的线号
- Gap Extender: `connector_male.size` 和 `connector_female.size` 自动跟随 `cu_wire`
- 导线为 `"none"` 时连接器也跟随为 `"none"`

**连接器公母属性规则**（`Connector` 模型本身不存公母，公母由生成时按极性/位置传入 `_make_connect_part` 的 `gender` 参数决定）:
- IBEX / Homerun Extender 正极产品：支线连接器（含末端支线）为 Male，外部主线连接器为 Female
- IBEX / Homerun Extender 负极产品：支线连接器（含末端支线）为 Female，外部主线连接器为 Male
- LYNX 产品：支线连接器与相连 IBEX 同极性产品的外部主线连接器相反（正极 Male、负极 Female）
- Gap Extender（仅生成负极产品）：第一个连接器为 Male（`connector_male`），最后一个连接器为 Female（`connector_female`）

**关键方法**:

| 方法 | 说明 |
|------|------|
| `__post_init__()` | 校验名称和类型；按 `branch_count` 规范化 IBEX/Homerun/Placeholder 类型；确保所有 Length 字段为 Length 实例；自动填充 branch_lines；同步连接器线号 |
| `_sync_connector_sizes()` | 将连接器线号与所连接导线同步 |
| `to_dict()` → `dict` | 序列化（所有字段序列化） |
| `from_dict(data, identifiers)` → `ProductPreset` | 反序列化（缺少字段时使用默认值填充） |

**约束**:
- IBEX 类型 `name` 必须为单个大写字母 (A-Z)，否则抛出 `ValueError`
- LYNX 类型 `name` 必须以 "L" 开头后跟一个或多个大写字母（如 "LA", "LB", "LABC"），否则抛出 `ValueError`
- Gap_Extender 类型 `name` 不能为空
- `product_type` 必须为 `"IBEX"`、`"Homerun_Extender"`、`"Placeholder"`、`"LYNX"` 或 `"Gap_Extender"`（兼容旧值 `"Extender"`），否则抛出 `ValueError`
- 所有 Connector / Fuse / Wire 字段均有 "none" 默认值，创建时无需显式提供

---

### 2.20 DataFillTarget / DataFillTerm / QuantityRef

**文件**: `models/data_fill.py`

凑数据目标的稀疏线性表达：`QuantityRef` 引用 Node、串、支架或产品基础量，`DataFillTerm` 保存其非零整数系数，`DataFillTarget` 保存有序目标的名称、目标值、启用状态及基础目标标识。组合公式只在 GUI 输入时存在，项目文件保存解析后的结构化项。

---

## 3. 视图状态模型 (View State Models)

视图状态均为 `@dataclass(frozen=True)` 不可变对象，用于 Qt 视图层数据绑定。位于 `gui/viewmodels/states.py`。

### 3.1 ListItemState

列表项状态，用于支架组合、产品预设等列表。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `item_id` | `str` | *(必填)* | 列表项唯一标识 |
| `label` | `str` | *(必填)* | 列表项显示文本 |
| `muted` | `bool` | `False` | 是否以弱化样式显示（如计数为 0 的项） |

### 3.2 AppState

应用全局状态。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `title` | `str` | `"Count and Design"` | 窗口标题 |
| `current_file` | `str \| None` | `None` | 当前打开的文件路径 |

---

### 3.3 CountTabState

Count Tab 的完整状态。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `tree` | `tuple[TreeNodeState, ...]` | `()` | 左侧树节点列表 |
| `selected_node_id` | `str \| None` | `None` | 当前选中节点的 ID |
| `editor` | `EditorState \| None` | `None` | 右侧编辑器状态 |

---

### 3.4 TreeNodeState

树节点状态，递归结构。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `node_id` | `str` | *(必填)* | 节点唯一标识 |
| `item_type` | `str` | *(必填)* | 类型: `"project"`, `"inv"`, `"node"`, `"row"` |
| `label` | `str` | *(必填)* | 显示文本 |
| `expanded` | `bool` | `True` | 是否展开 |
| `children` | `tuple[TreeNodeState, ...]` | `()` | 子节点 |

---

### 3.5 EditorState

编辑面板状态，根据当前选中类型 (`kind`) 显示不同字段。

| 字段 | 类型 | 默认值 | 适用类型 | 说明 |
|------|------|--------|----------|------|
| `kind` | `str` | *(必填)* | 全部 | 编辑对象类型 |
| `title` | `str` | *(必填)* | 全部 | 编辑面板标题 |
| `name` | `str` | `""` | 全部 | 对象名称 |
| `address` | `str` | `""` | project | 项目地址 |
| `client_name` | `str` | `""` | project | 客户名称 |
| `project_type` | `str` | `""` | project | 项目类型 (IBEX/LYNX) |
| `racking_pitch` | `str` | `""` | project/inv/node | 支架间距（字符串形式） |
| `lynx_preset_name` | `str` | `""` | project/inv/node | 当前选中的 LYNX preset 名称 |
| `lynx_preset_options` | `tuple[str, ...]` | `()` | project/inv/node | 可选的 LYNX preset 名称列表 |
| `cluster_length` | `str` | `""` | node | Cluster 延长长度 |
| `box_position` | `float` | `0.0` | node | 箱子位置 |
| `racking_entries` | `tuple[tuple[str, str], ...]` | `()` | row | 支架组合条目列表，每项为 `(rackings_name, offset)` |
| `rackings_options` | `tuple[str, ...]` | `()` | row | 可选支架组合列表 |
| `identifier_options` | `tuple[tuple[str, str], ...]` | `()` | project/inv/node | 可选长度标识符（(名称, 表达式) 对） |
| `sections` | `tuple[TableSectionState, ...]` | `()` | 全部 | 统计信息分段表 |

---

### 3.6 TableSectionState

表格分段状态。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `title` | `str` | *(必填)* | 分段标题 |
| `headers` | `tuple[str, ...]` | *(必填)* | 列标题 |
| `rows` | `tuple[tuple[str, ...], ...]` | *(必填)* | 数据行 |

---

### 3.7 RackingsTabState / RackingsDetailState

Rackings Tab 状态。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `items` | `tuple[ListItemState, ...]` | `()` | 左侧支架组合列表 |
| `selected_item_id` | `str \| None` | `None` | 选中项 ID |
| `detail` | `RackingsDetailState` | `RackingsDetailState()` | 右侧详情 |

**RackingsDetailState**:

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | `str` | `""` | 支架组合名称 |
| `product_presets` | `str` | `""` | IBEX 产品字符串 |
| `usage_count` | `int` | `0` | 被 Row 引用的次数 |
| `gap_extenders` | `tuple[tuple[str, str], ...]` | `()` | Gap Extender 列表，每项为 `(名称, 数量)` |
| `existing_gap_extender_names` | `tuple[str, ...]` | `()` | 项目中所有已有的 Gap Extender 名称（用于下拉选择） |

---

### 3.8 ProductPresetsTabState

Product Presets Tab 状态。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `items` | `tuple[ListItemState, ...]` | `()` | 左侧产品预设列表（label 为名称） |
| `selected_item_id` | `str \| None` | `None` | 选中项 ID |
| `detail_title` | `str` | `"Product Preset Properties"` | 详情面板标题 |
| `detail` | `ProductPresetDetailState` | `ProductPresetDetailState()` | 右侧详情 |

---

### 3.9 ProductPresetDetailState

ProductPreset 详情编辑器的状态，根据 `product_type` 显示不同的字段组。

**核心字段**:

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | `str` | `""` | 产品预设名称 |
| `product_type` | `str` | `"IBEX"` | 产品类型 (IBEX/LYNX/Gap_Extender) |
| `remarks` | `str` | `""` | 备注 |
| `pv_panel` | `str` | `""` | 光伏面板名称 |

**IBEX 字段** (当 `product_type == "IBEX"` 时显示):

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `branch_count` | `str` | `"1"` | 支线数量（可编辑） |
| `branch_connector_type` | `str` | `""` | 支线连接器类型 |
| `branch_connector_manufacturer` | `str` | `""` | 支线连接器制造商 |
| `branch_connector_size` | `str` | `""` | 支线连接器线号 |
| `branch_connector_material` | `str` | `"Cu"` | 支线连接器材料 |
| `branch_connector_strands` | `str` | `"none"` | 支线连接器股数 |
| `external_connector_type` | `str` | `""` | 外部主线连接器类型 |
| `external_connector_manufacturer` | `str` | `""` | 外部主线连接器制造商 |
| `external_connector_size` | `str` | `""` | 外部主线连接器线号 |
| `external_connector_material` | `str` | `"Cu"` | 外部主线连接器材料 |
| `external_connector_strands` | `str` | `"none"` | 外部主线连接器股数 |
| `branch_fuse_current` | `str` | `""` | 支线保险丝电流 |
| `branch_fuse_manufacturer` | `str` | `""` | 支线保险丝制造商 |
| `last_branch_fuse_current` | `str` | `""` | 末端支线保险丝电流 |
| `last_branch_fuse_manufacturer` | `str` | `""` | 末端支线保险丝制造商 |
| `last_branch_cu_wire` | `str` | `""` | 末端支线铜线号 |
| `last_branch_cu_material` | `str` | `"Cu"` | 末端支线铜线材料 |
| `last_branch_cu_strands` | `str` | `"none"` | 末端支线铜线股数 |
| `last_branch_al_wire` | `str` | `""` | 末端支线铝线号 |
| `last_branch_al_material` | `str` | `"Al"` | 末端支线铝线材料 |
| `last_branch_al_strands` | `str` | `"none"` | 末端支线铝线股数 |
| `external_fuse_current` | `str` | `""` | 外部主线保险丝电流 |
| `external_fuse_manufacturer` | `str` | `""` | 外部主线保险丝制造商 |
| `branch_cu_wire` | `str` | `""` | 支线铜线号 |
| `branch_cu_material` | `str` | `"Cu"` | 支线铜线材料 |
| `branch_cu_strands` | `str` | `"none"` | 支线铜线股数 |
| `branch_al_wire` | `str` | `""` | 支线铝线号 |
| `branch_al_material` | `str` | `"Al"` | 支线铝线材料 |
| `branch_al_strands` | `str` | `"none"` | 支线铝线股数 |
| `external_cu_wire` | `str` | `""` | 外部主线铜线号 |
| `external_cu_material` | `str` | `"Cu"` | 外部主线铜线材料 |
| `external_cu_strands` | `str` | `"none"` | 外部主线铜线股数 |
| `external_al_wire` | `str` | `""` | 外部主线铝线号 |
| `external_al_material` | `str` | `"Al"` | 外部主线铝线材料 |
| `external_al_strands` | `str` | `"none"` | 外部主线铝线股数 |
| `internal_cu_wire` | `str` | `""` | 内部主线铜线号 |
| `internal_cu_material` | `str` | `"Cu"` | 内部主线铜线材料 |
| `internal_cu_strands` | `str` | `"none"` | 内部主线铜线股数 |
| `internal_al_wire` | `str` | `""` | 内部主线铝线号 |
| `internal_al_material` | `str` | `"Al"` | 内部主线铝线材料 |
| `internal_al_strands` | `str` | `"none"` | 内部主线铝线股数 |
| `product_total_length` | `str` | `""` | 跨产品所需长度 |
| `branch_lines` | `tuple[tuple[str, str, str], ...]` | `()` | 支线坐标列表，每项为 `(index, positive, negative)` |
| `length_displays` | `dict[str, str]` | `{}` | 长度字段到转换后显示字符串的映射（如 `"≈ 304.8 mm"`） |

**IBEX 默认长度参数**:

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `branch_length_positive` | `str` | `""` | 支线长度（正） |
| `branch_length_negative` | `str` | `""` | 支线长度（负） |
| `last_branch_with_extra_length` | `bool` | `False` | 最后一条支线是否额外加长度 |
| `branch_fuse_prefix` | `str` | `""` | 支线保险丝前缀距离 |
| `branch_first_cual_prefix` | `str` | `""` | 末端支线铜铝头到前一个 T-mold 间距 |
| `branch_second_cual_suffix` | `str` | `""` | 末端支线铜铝头到连接器间距 |
| `external_fuse_suffix` | `str` | `""` | 外部保险丝后缀距离 |
| `external_cu_length_to_row_head` | `str` | `""` | 外部铜线到排头距离 |
| `external_second_cual_suffix` | `str` | `""` | 外部第二个铜铝转接器后缀 |
| `external_extension_trench_length` | `str` | `""` | 外部穿管长度 |
| `internal_first_cual_prefix` | `str` | `""` | 内部第一个铜铝转接器前缀 |
| `internal_second_cual_suffix` | `str` | `""` | 内部第二个铜铝转接器后缀 |
| `external_al_threshold` | `str` | `""` | 外部主线铜铝转接阈值 |
| `last_branch_al_threshold` | `str` | `""` | 末端支线铜铝转接阈值 |
| `internal_al_threshold` | `str` | `""` | 内部主线铜铝转接阈值 |

**LYNX 字段** (当 `product_type` 为 `"LYNX"` 时显示):

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `cluster_wire` | `str` | `""` | LYNX Cluster 延长线线号 |
| `cluster_wire_material` | `str` | `"Al"` | LYNX Cluster 延长线材料 |
| `cluster_wire_strands` | `str` | `"none"` | LYNX Cluster 延长线股数 |
| `branch_length_cu` | `str` | `""` | 支线铜长度 |
| `branch_length_al` | `str` | `""` | 支线铝长度 |

**Gap Extender 字段** (当 `product_type` 为 `"Gap_Extender"` 时显示):

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `connector_male_type` | `str` | `""` | 公头连接器类型 |
| `connector_male_manufacturer` | `str` | `""` | 公头连接器制造商 |
| `connector_male_size` | `str` | `""` | 公头连接器线号 |
| `connector_female_type` | `str` | `""` | 母头连接器类型 |
| `connector_female_manufacturer` | `str` | `""` | 母头连接器制造商 |
| `connector_female_size` | `str` | `""` | 母头连接器线号 |
| `label` | `str` | `""` | 标签 |
| `cu_wire` | `str` | `""` | 铜线线号 |
| `cu_wire_strands` | `str` | `"none"` | 铜线股数 |
| `length` | `str` | `""` | Gap Extender 长度 |

---

### 3.10 LengthIdentifiersTabState

Length Identifiers Tab 状态。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `items` | `tuple[LengthIdentifierItemState, ...]` | `()` | 左侧标识符列表 |
| `selected_item_id` | `str \| None` | `None` | 选中项 ID |
| `identifier_options` | `tuple[tuple[str, str], ...]` | `()` | 标识符选项 |
| `detail` | `LengthIdentifiersDetailState` | `LengthIdentifiersDetailState()` | 右侧详情 |

**LengthIdentifierItemState**:

| 字段 | 类型 | 说明 |
|------|------|------|
| `item_id` | `str` | 标识符 ID |
| `label` | `str` | 显示标签 |

**LengthIdentifiersDetailState**:

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | `str` | `""` | 标识符名称（字母/下划线组成） |
| `length` | `str` | `""` | 长度表达式 |

---

## 4. 服务层辅助类型

### 4.1 ParentContext

**文件**: `application/services/tree.py`

用于在树操作中定位父节点。

| 字段 | 类型 | 说明 |
|------|------|------|
| `item_type` | `str` | 当前项类型 |
| `item` | `object` | 当前项实例 |
| `parent` | `object \| None` | 父项实例（project 无父项） |

---

### 4.2 RackingsRenameResult

**文件**: `application/services/rackings.py`

支架重命名操作的结果。

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | `str` | 结果状态: `"invalid"`, `"noop"`, `"needs_confirmation"`, `"merged"`, `"renamed"` |
| `message` | `str` | 操作消息 |
| `requires_confirmation` | `bool` | 是否需要用户确认 |

---

### 4.3 ProductGenerationResult

**文件**: `application/product_generation/result.py`

产品生成操作的结果容器，包含半成品、成品以及 IBEX/LYNX/Gap Extender 产品的组装结果。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `semi_parts` | `list[Part]` | `[]` | 半成品部件列表 |
| `parts` | `list[Part]` | `[]` | 成品部件列表 |
| `ibex_products` | `list[AssembledProduct]` | `[]` | IBEX 组装产品列表 |
| `lynx_products` | `list[AssembledProduct]` | `[]` | LYNX 组装产品列表 |
| `gap_extender_products` | `list[AssembledProduct]` | `[]` | Gap Extender 组装产品列表 |
| `homerun_extender_products` | `list[AssembledProduct]` | `[]` | Homerun Extender 组装产品列表 |
| `all_products` | `list[AssembledProduct]` | `[]` | 去重前、带来源元数据的全部产品实例 |
| `racking_instances` | `list[GeneratedRackingInstance]` | `[]` | 每个实际 RackingEntry 与其正负极产品对的关联，供 SVG 简图导出使用 |

**GeneratedRackingInstance** 记录 INV、Node、Row、RackingEntry 序号、支架组合名称、
preset 名称顺序及 `GeneratedPresetProducts` 列表。`GeneratedPresetProducts` 记录 preset
在支架组合中的位置、实际产品类型，以及对应的正极和负极 `AssembledProduct`；未生成
产品时正负极字段为 `None`。

`export_schematics(result, project, directory, progress_callback=None)` 根据
`racking_instances` 生成去重后的 SVG 简图，并返回包含目录、文件列表、变体数量和
非致命警告的 `SchematicExportResult`。

---

## 5. 类型关系图

```
Project
├── length_identifiers: list[LengthIdentifier]
│   └── length: Length
├── rackings_list: list[Rackings]
│   ├── product_presets: str → 引用 ProductPreset.name (IBEX/Homerun/Placeholder)
│   └── gap_extenders: list[(name, quantity)] → 引用 ProductPreset.name (Gap_Extender)
├── product_presets: list[ProductPreset]
├── data_fill_targets: list[DataFillTarget]
│   └── terms: list[DataFillTerm] → QuantityRef
├── racking_pitch: Length (全局默认)
├── cluster_wire: Wire (全局默认)
├── lynx_preset: str → 引用 ProductPreset.name (全局默认 LYNX preset)
└── invs: list[Inv]
    ├── racking_pitch: Length (INV级，覆盖全局)
    ├── cluster_wire: Wire (INV级，覆盖全局)
    ├── lynx_preset: str → 引用 ProductPreset.name (INV级，覆盖全局)
    └── nodes: list[Node]
        ├── racking_pitch: Length (Node级，最高优先级)
        ├── cluster_wire: Wire (Node级，最高优先级)
        ├── lynx_preset: str → 引用 ProductPreset.name (Node级，最高优先级)
        ├── cluster_length: Length
        └── rows: list[Row]
            └── racking_entries: list[RackingEntry]
                ├── rackings: str → 引用 Rackings.name
                └── offset: Length

ProductPreset (挂载在 Project.product_presets 下)
├── 公共字段:
│   ├── branch_cu_wire: Wire
│   ├── branch_al_wire: Wire
│   ├── pv_panel: str
│   └── remarks: str
├── if product_type in {"IBEX", "Homerun_Extender"}:
│   ├── branch_lines: list[IBEXBranchCoordinate]
│   │   ├── coordinate_positive: Length
│   │   └── coordinate_negative: Length
│   ├── product_total_length: Length
│   ├── branch_connector: Connector
│   │   └── size: Wire (自动同步 branch_cu_wire)
│   ├── branch_fuse: Fuse
│   ├── external_cu_wire: Wire
│   ├── external_al_wire: Wire
│   ├── external_fuse: Fuse
│   ├── external_connector: Connector
│   │   └── size: Wire (自动同步 external_cu_wire)
│   ├── internal_cu_wire: Wire
│   ├── internal_al_wire: Wire
│   └── (+ 13 个 Length 默认参数)
├── if product_type == "Placeholder":
│   └── product_total_length: Length（仅供后续产品累计）
├── if product_type == "LYNX":
│   ├── branch_length_cu: Length
│   ├── branch_length_al: Length
│   └── cluster_wire: Wire
└── if product_type == "Gap_Extender":
    ├── connector_male: Connector
    │   └── size: Wire (自动同步 cu_wire)
    ├── connector_female: Connector
    │   └── size: Wire (自动同步 cu_wire)
    ├── cu_wire: Wire
    ├── length: Length
    └── label: str

IBEXProduct (生成流程模型)
├── extender_length: Length
├── whip_length: Length
└── branches: list[IBEXBranch]
    └── prefix_length: Length

LYNXProduct (生成流程模型)
├── mainline_wire: Wire
└── overmolds: list[Overmold]
    ├── branch_lines: list[LYNXBranch]
    │   ├── cu_wire: Wire
    │   ├── al_wire: Wire
    │   ├── cu_length: Length
    │   └── al_length: Length
    └── prefix_length: Length

Part (部件模型，由零件工厂创建)
├── code: str
├── category: str
├── description: str
├── unit: str
├── overmolding_detail: str
├── label_detail: str
├── direction_nos: list[int]
├── length: float
├── semi_or_not: bool
└── semi_list: list[Part]  ← 递归嵌套

AssembledProduct (组装产品，生成结果)
├── code: str
├── polarity: str  ("POS" / "NEG")
├── quantity: int
├── description: str
├── pv_panel: str
└── parts: list[Part]

RackingEntry (Row 内的支架条目)
├── rackings: str → 引用 Rackings.name
└── offset: Length

Connector  ← 独立原子类型
Fuse       ← 独立原子类型
Wire       ← 独立原子类型，被 Connector 及多种模型引用
Length     ← 独立原子类型，被几乎所有模型引用
```

**Length ↔ LengthIdentifier 的关系**:
- `Length` 持有 `identifiers: list[LengthIdentifier]`
- 当 `length_str` 中包含标识符名称时，Length 会在 `identifiers` 中查找对应 `LengthIdentifier`，取其 `length` 值参与计算
- `Project._bind_length_identifiers()` 负责遍历所有 Length 并注入标识符引用
- 支持循环依赖检测：`Project.would_create_length_identifier_cycle()` 使用三色 DFS 检测
- 支持批量重命名引用：`Project.replace_length_identifier_references()` 基于 token 级别安全替换
