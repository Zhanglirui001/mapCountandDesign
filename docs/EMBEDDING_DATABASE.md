# 图纸类别向量数据库

运行时参考库为 `src/count_and_design/resources/embeddings/category_index.sqlite`。
包含 REVAMP 34 类、72 个样本，以及 EVS 52 类、2452 个样本，共 86 类、2524 个样本。
类别均值另存，共 86 个。向量均为 2048 维，使用小端 float64 BLOB，导入不降低精度。

类别名称由项目类型和原始类别名称组成，例如 `REVAMP+array_layout`、
`EVS+ARRAY LAYOUT`。保留原始类别拼写，包括 EVS 的 `OHTER`。
两套库的 ARRAY LAYOUT 对应 `Array Layout`，OVERALL SITE LAYOUT 对应
`Overall Layout`；`DC COLLECTION SYSTEM SUMMARY` 和 `summary table` 也对应
`Overall Layout`，其他类别对应 `Other`。这项映射保存在 categories.page_type。
识别结果的 purpose 和 matched_category 保留完整的带项目前缀的类别名称。

## 存储结构

- `categories`：项目类型、原始类别、唯一完整名称、页面类型、维度、数据类型、
  原始类别均值、embedding_provider、来源路径和来源元数据。
- `samples`：所属类别、原始图片名称、图片哈希、原始 embedding。
  同一类别内图片名称唯一，删除类别会级联删除样本。
- `PRAGMA user_version = 1`：数据库格式版本。

来源路径仅用于追溯；运行时不需要访问 EVS 外部目录。
EVS 提供的模型信息为 `internal:./models/Qwen3-VL-Embedding-2B|rs1080`。
REVAMP 原 JSON 未记录模型/预处理信息，因此原有类别的 provider 为 NULL。
新增 `REVAMP+summary table` 已记录实际模型、提示词、输入尺寸、哈希算法及生成时间。
相同维度不能证明向量空间相同；后续更换服务或模型时，需要确认两套参考库的生成
模型、prompt、预处理与查询一致，或重新生成参考向量。导入不会自动转换向量空间。

## 重建数据库

在项目根目录执行（仅重建最初的 85 类来源库，会替换输出文件并丢弃后续新增类别，
包括 `REVAMP+summary table`；应先备份当前数据库）：

```powershell
.\.venv\Scripts\python.exe test_scripts/build_category_database.py `
  --revamp src/count_and_design/resources/embeddings/category_index.json `
  --evs C:/Data/2D_design/evs_embeddings `
  --output src/count_and_design/resources/embeddings/category_index.sqlite
```

脚本读取 REVAMP 汇总 JSON 和 EVS 各类别的 `hash_map.json`，校验所有向量，
在临时数据库完成导入和完整性检查后替换输出。原 JSON 保留为迁移来源，
默认加载与 PyInstaller 打包使用 SQLite。

## 增删类别与样本

### 直接调用模型新增图片类别

`test_scripts/add_category_embeddings.py` 读取指定目录直属的 PNG，不递归读取备份目录，
使用项目 `.env` 中的 embedding 模型和空用户提示词，对所有样本成功生成并校验向量后，
在一个事务中写入类别、各样本向量、SHA-256 哈希和原始向量的算术平均值。
已有同名类别时会拒绝操作，避免意外覆盖或重复调用模型。

`REVAMP+summary table` 已按以下命令生成并写入（四张原始 PNG 均为 1080×720）：

```powershell
.\.venv\Scripts\python.exe test_scripts/add_category_embeddings.py `
  --database src/count_and_design/resources/embeddings/category_index.sqlite `
  --images 'C:/Data/2D_design/revamp_category/summary table' `
  --project-type REVAMP `
  --source-type 'summary table'
```

### 存储 API

`application.ai_statistics.vector_store.CategoryVectorStore` 提供：

- `import_category(...)`：新增类别及其初始样本，名称自动加项目前缀。
- `upsert_sample(...)`：新增或替换样本，并在同一事务内重算类别均值。
- `delete_sample(...)`：删除样本并重算均值；无样本的类别保留但不参与分类。
- `delete_category(...)`：删除类别和全部样本。

导入保留来源均值；编辑后均值按该类别当前原始样本向量的算术平均重算。
向量维度错误、非有限值、零向量或重算后的零均值会导致整个编辑事务回滚。
实际编辑 API 目前没有 GUI，调用方需要操作可写的数据库文件。
打包资源以只读方式加载；用户编辑库应复制到持久的用户数据目录，
然后通过 `load_category_index(path)` 加载，并传入分类 provider 的 `library` 参数。

分类仍使用 NumPy，完整保留“类别均值相似度”和“最佳样本相似度”取最大值的算法。
默认参考库通过 `bundled_category_index()` 缓存。编辑后需要重新加载库；
如修改了默认库，先调用 `bundled_category_index.cache_clear()`，
再重新创建分类任务，避免正在执行的任务混用不同版本。
