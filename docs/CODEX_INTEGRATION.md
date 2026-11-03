# Codex 接入、识别与复核

本文面向 Windows 打包版 Count and Design。一个 EXE 同时提供桌面界面和 MCP 服务，无需安装 Python、uv 或项目源码。

接入后，可以在本地 Codex 桌面应用、CLI 或 IDE 扩展中要求：启动 Count and Design → 导入 PDF → 执行识别 → 查看疑点原图 → 提交修正。应用负责重算、保存修正记录和刷新界面。

## 首次接入：按这 5 步操作

### 第 1 步：放置桌面 EXE

把打包后的 EXE 放在准备长期使用的位置。下文以本次生成的文件为例：

```text
C:\Data\2D_design\count-and-design\dist\Count and Design v1.7.10.exe
```

实际位置或版本号不同时，修改下方 `command` 为实际 EXE 的绝对路径。移动 EXE 或升级后文件名变化，也需要同步更新该路径。

### 第 2 步：打开 Codex 配置文件

在 PowerShell 中执行：

```powershell
notepad "$env:USERPROFILE\.codex\config.toml"
```

如果目录或文件不存在，可以创建 `.codex` 文件夹及 `config.toml`，避免保存成 `config.toml.txt`。如果设置过 `CODEX_HOME`，编辑该目录中实际使用的 `config.toml`。

这里修改的是用户目录下的 Codex 配置。直接在 ChatGPT 网页填写本地配置不会生效。

### 第 3 步：添加或替换 Count and Design 配置

已有 `[mcp_servers.count-and-design]` 时，替换整个旧表，并删除对应的 `[mcp_servers.count-and-design.env]` 旧子表。保留其他 MCP 服务和 Codex 设置，不要重复添加同名表。

```toml
[mcp_servers.count-and-design]
command = 'C:\Data\2D_design\count-and-design\dist\Count and Design v1.7.10.exe'
args = ['--mcp']
startup_timeout_sec = 120
tool_timeout_sec = 180
```

直接保留示例中的英文单引号和反斜杠。可复制仓库中的 [打包版 MCP 配置示例](../integrations/codex/mcp-config.packaged.example.toml)。

| 配置项 | 作用 |
| --- | --- |
| `command` | 启动包含 MCP 服务的桌面 EXE，必须使用实际绝对路径 |
| `args` | `--mcp` 让同一个 EXE 以 STDIO MCP 服务模式运行 |
| `startup_timeout_sec` | MCP 服务启动等待时间，单位为秒 |
| `tool_timeout_sec` | 单次工具调用等待时间，单位为秒 |

从源码版迁移时，移除旧的 Python 启动路径、模块参数、`cwd` 和 `COUNT_DESIGN_PROJECT_ROOT`；打包版默认不需要这些设置。

MCP 接入配置不需要额外填写 OpenAI API Key。实际识别仍依赖应用的 OCR、LLM、Embedding 服务配置及本地模型。打包版支持读取内置配置，也支持通过 EXE 旁的 `.env` 覆盖配置；进程已有的环境变量优先。

### 第 4 步：保存配置并重启 Codex

保存后完全退出并重新打开 Codex；若使用 IDE 扩展，重启扩展或重新加载窗口，然后新开一个本地聊天。

两种应用启动方式都支持：

- 先双击 EXE，再让 Codex 调用 `start_session` 连接当前窗口。
- 直接让 Codex 调用 `start_session`，应用尚未运行时会自动启动。

Codex 根据配置启动 MCP 服务；注册 MCP 本身不会立即打开桌面窗口。双击 EXE 会自动开启私有本地应用桥接，无须另外启动服务或配置 HTTP 端口。

### 第 5 步：发送一句话验证接入

在新的 Codex 聊天中发送：

> 请调用 count-and-design 的 start_session，连接或启动 Count and Design，并告诉我是否连接成功。

成功时，工具返回包含 `window_id`、`project_id`、`revision`、`page_count` 等字段的状态。没有选中窗口时，`start_session()` 连接唯一已打开的窗口，或在没有窗口时新建一个；已有多个窗口时，必须明确选择。选中窗口后，重复调用会复用该窗口。

应以实际工具调用及返回结果为准，仅回复“可以启动”或输出命令不能证明接入成功。

## 多窗口与识别队列

每次普通双击 EXE 都会新开一个独立窗口。每个窗口拥有独立的桥接地址、认证令牌、项目状态和复核数据；窗口标题显示 `window_id`，便于区分。

同一个 Codex 会话可使用以下工具管理窗口：

| 调用 | 行为 |
| --- | --- |
| `start_session(new_window=True)` | 新建独立窗口，并将后续工具调用指向该窗口 |
| `list_sessions()` | 列出当前可连接窗口的 ID、项目/PDF 状态和当前选择 |
| `start_session(window_id="列表中的 ID")` | 切换到指定窗口，后续导入、识别和修正均针对该窗口 |
| `start_session()` | 复用本 MCP 会话已选窗口；首次连接存在多个窗口时要求明确选择 |
| `cancel_recognition()` | 取消当前所选窗口中排队或运行的识别阶段 |

例如可以告诉 Codex：“新建两个独立窗口，分别导入 A.pdf 和 B.pdf，并依次提交识别。”Codex 应记住每份图纸对应的 `window_id`，切换后核对 `project_id`，再继续操作。预览和提交修正期间应保持选中同一窗口。

Agent1、Agent2 在各窗口独立运行。为限制内存叠加，Agent3 跨窗口共用 FIFO 队列，同时只运行一个，其余等待。窗口一运行 Agent3 时，窗口二仍可立即运行 Agent1 或 Agent2；窗口二提交的 Agent3 则显示排队。同一窗口同时只运行一个阶段。一个任务是当前 PDF 的一个阶段；阶段内部仍按现有规则处理多页。界面按钮和 MCP 调用遵循相同规则。

`start_recognition` 接受请求后立即返回 `job_id`，通过 `get_job_status(job_id=...)` 查询。`job.status` 区分 `queued`、`running`、`completed`、`failed` 和 `cancelled`；`queue_position` 表示当前阶段所属队列的前方任务数，运行时为 0。Agent3 的前方任务可以来自其他窗口，Agent1/2 不受该共享队列影响。取消按钮可移除等待中的任务。

运行任务完成、失败或取消且工作线程实际停止后才释放名额；关闭等待窗口会移除其排队任务。进程异常退出留下的失效队列项会在后续调度时清理。Codex 看图、复核和修正不占识别名额。等待期间不能替换该窗口的 PDF；可取消等待，或切换到其他独立窗口操作。

Agent3 子进程会监测主窗口进程的退出，并持有独立的任务租约；即使主窗口崩溃，子进程实际退出前仍不会放行下一任务。数据库暂时被占用时，等待中的任务会保留位置并自动重试，取消操作不依赖数据库写锁。排队期间可以浏览已开放阶段，任务开始时自动切回原来提交的识别阶段。

## 接入成功后，如何开始识别

### 方式 A：在应用窗口选择 PDF

双击 EXE，或通过 `start_session` 打开应用，然后在该窗口上传 PDF。上传完成后回到 Codex，说：

> 我已在 Count and Design 中上传 PDF。请读取当前项目上下文，执行识别，查看疑点的原图，自动修正证据明确的问题，并报告修改内容和剩余问题。

### 方式 B：把本地 PDF 路径告诉 Codex

例如发送以下内容，把示例路径替换成真实存在的文件：

> 请使用 count-and-design 工具导入 `C:\drawings\project.pdf`，执行识别，然后查看疑点原图并修正证据明确的错误。不要为了让数量相等而强行补框，最后给出修改摘要和剩余问题。

不必自己输入 `import_pdf`、`preview_corrections` 等 JSON 参数，Codex 会根据工具说明调用。要求另开或导入新项目时，Codex 应先调用 `start_session(new_window=True)`，保留已有窗口。`import_pdf` 默认拒绝覆盖已导入的 PDF，即使尚未开始识别；只有明确要求替换当前图纸时才传入 `replace_existing=True`。同一路径重复导入也受此保护。

若已知连接方向，可以补充“连接方向为 P-N”等项目信息。未提供方向时，工具保留 unknown，并在摘要中提示极性/偏移解释仍需复核。

### 可选：让 Codex 使用配套复核 Skill

MCP 提供操作能力，Skill 规定复核流程。只接入 MCP 就能调用工具；使用配套 Skill 可以让后续任务遵循一致的检查步骤。

以下为可选开发资源，需要单独取得配套 Skill 文件；只持有 EXE 不影响 MCP 使用。已取得文件时，可以告诉 Codex：

> 请先读取 `C:\Data\2D_design\count-and-design\integrations\codex\skills\count-design-review\SKILL.md`，按其中流程识别并复核当前 PDF。

如果希望长期使用，可将整个 `count-design-review` 文件夹复制到个人技能目录：

```text
C:\Users\LiruiZhang\.codex\skills\count-design-review\SKILL.md
```

即把文件夹放在 `.codex\skills` 下，保留其中的 `SKILL.md`。重启后，在支持 Skill 提及的 Codex 输入框中选择 `count-design-review`，或明确要求使用该流程。

## 常见问题：先按现象排查

| 现象 | 检查方法 |
| --- | --- |
| 提示 EXE 不存在或无法启动 | 确认 `command` 指向实际 EXE，检查文件移动或版本文件名变化，并确认使用本次支持 `--mcp` 的打包版本。 |
| 提示 TOML 重复定义或配置解析失败 | 只保留一个 `[mcp_servers.count-and-design]`，删除旧 `.env` 子表；检查中文引号或 `.toml.txt` 扩展名。 |
| 仍提示 Python 或模块不存在 | Codex 仍在使用旧的源码配置；按第 3 步替换配置，重启 Codex 并新开会话。打包版无需安装 Python。 |
| Codex 找不到 `start_session` | 检查是否编辑了实际使用的用户配置，以及配置是否保存、Codex 是否完全重启。 |
| MCP 已连接，但应用未启动 | 让 Codex 实际调用 `start_session`。只有 MCP 握手不会打开应用窗口。 |
| `start_session` 报启动失败 | 查看 `%LOCALAPPDATA%\CountAndDesign\runtime\windows\<实例目录>\app.log`，或工具错误中给出的完整日志路径；双击启动的应用不一定生成该文件。 |
| 窗口打开了，但识别失败 | 查询 `get_job_status` 的具体错误，再检查应用服务配置、本地模型和 OCR/LLM/Embedding 连接。 |
| 已有多个窗口，提示选择窗口 | 调用 `list_sessions`，核对 PDF 与项目后，使用 `start_session(window_id=...)` 选择。 |
| 某个窗口一直显示排队 | 查询该窗口的 `get_job_status`。前方阶段结束后会自动开始；如需放弃，调用 `cancel_recognition` 或在该窗口点击取消。 |
| 手动修改后出现 revision 冲突 | 让 Codex 重新读取当前证据并生成修正预览。 |

不要在普通终端用 `--mcp` 检查窗口是否出现：该模式等待 MCP 客户端的 STDIO 消息。日常打开界面直接双击即可，完整接入按第 5 步验证。

## 运行数据与更新

Windows 打包版每个窗口默认使用 `%LOCALAPPDATA%\CountAndDesign\runtime\windows\<实例目录>`：

- `bridge.json`：本地桥接地址、进程 ID 和认证令牌，正常关闭应用时删除。
- `app.log`：由 MCP 自动启动桌面应用时记录的标准输出及错误日志。
- `projects/<project_id>`：复核结果、证据和修正审计；应用关闭后仍保留。

运行数据保存在用户目录，支持将 EXE 放在不允许普通用户写入的安装目录。Agent1/2 识别租约保存在 `%LOCALAPPDATA%\CountAndDesign\recognition-queue\windows` 下的各窗口子目录；Agent3 使用同一根目录下的 `agent3` 共享队列。

通常无需设置环境变量。开发调试时可使用 `COUNT_DESIGN_BRIDGE_FILE` 固定某个桥接路径：相同路径仍只对应一个窗口。`COUNT_DESIGN_QUEUE_DIR` 可覆盖识别租约的根目录；使用同一根目录的窗口共用 Agent3 队列，Agent1/2 仍使用独立子目录。普通使用应保留默认值。所有窗口须使用更新后的程序；旧版无限制并行窗口不受新队列管理。

更新 EXE 前先导出需要保留的识别结果，再关闭旧应用及对应 MCP 会话。替换文件后检查 `command` 路径并重启 Codex。当前识别草稿不会在重启后自动恢复，审计文件保留不等于界面会自动恢复项目。

## 源码开发与重新打包

开发者使用的 Python 接入配置、依赖安装、调试和打包命令已移至 [源码开发接入说明](CODEX_SOURCE_DEVELOPMENT.md)。普通打包版用户按本文的 EXE 配置操作即可。

## 开发参考：一次完整工具调用

当前提供 14 个 MCP 工具，包含窗口列表、识别取消，以及识别与复核工具。下面的参数示例供开发和排查使用，普通用户可以直接通过上面的自然语言提示词操作。

1. `start_session()`：连接已有桥接实例或启动带界面的实例。
2. 在应用上传 PDF，或 `import_pdf(path="绝对路径")`。
3. `get_project_context()` 查看目标、Tracker 定义和业务规则。
4. `start_recognition(stage="all")` 只选择下一个待运行阶段，返回 job_id；用 `get_job_status(job_id)` 查询。每个阶段完成后必须核验，不能一次自动跑完三个 Agent。显式 stage=0/1/2 分别运行分类、Overall 信息、Layout 元素。若已知连接方向，可传 `panel_orientation="P-N"`（还支持 P-S/P-E/P-W/P-B/N-B）；未知时不要猜测。
5. `list_review_issues()`，随后 `get_review_evidence(page_number=12)`。工具返回真实 MCP ImageContent 原图和标注图，以及当前 revision、实体、表格与 evidence_id。小符号使用 bbox 请求局部图片。
6. 提交预览，例如：

```json
{
  "project_id": "从上下文读取",
  "base_revision": 17,
  "evidence_ids": ["从图片工具读取"],
  "operations": [
    {
      "op": "add_detection",
      "page_number": 12,
      "kind": "Tracker",
      "bbox": [0.21, 0.35, 0.24, 0.62],
      "parent_id": "从检测数据读取的 Node ID",
      "reason": "原图存在一条独立 Tracker，现有检测框遗漏"
    }
  ]
}
```

7. 检查预览的 changed_nodes、issues 和 validation，再以同一 project_id/base_revision 调用 `apply_corrections(preview_id=..., request_id="本次唯一标识")`。
8. `get_review_summary()` 输出修正记录、剩余疑点、Agent4 比较和保存路径。

## 三个 Agent 怎样核验和写回

流程为：Agent1 → Codex 核验分类 → Agent2 → Codex 核验基础数据 → Agent3 → Codex 定位与修正 → Agent4 校验。

默认采用快速结果审阅：直接读取已有识别结果，先列出“页码、可疑点、原因”，正常页不重复看图。用户只要疑点清单时，输出清单即结束；需要确认或修正具体疑点时，才读取对应原图或局部截图。不得把“已根据结果筛查”描述为“已逐页核对原图”。

**Agent1：** `get_project_context().agent1_review` 返回 `overall_pages`、`layout_pages`、每页 score/threshold/category_scores、阈值上下 0.05 范围候选页、Layout 中断页和边界邻页。`suspicious_pages` 附带疑点原因，`review_pages` 仅含中断页和近阈值页，是候选清单，不要求全部读取图片；边界邻页仅供需要时参考。分数是 embedding 相似度，不是校准后的概率；连续性只能辅助判断。

快速调用：使用 `get_project_context(detail="compact", stage=0)`，保留所有页面和疑点信息，每页只返回前三个分类候选；`detail="full"` 可读取全部候选及原始诊断。`stage=1/2` 分别只加载 Agent2/3 的复核区段，省略 stage 则返回所有区段。Agent2 extraction 仍保留完整结构。

同一会话和 revision 下，已查看原图的 evidence_id 可以复用，`available_evidence` 列出当前仍有效的 ID。后续“修改刚才四页”只需确认版本、复用证据、批量预览、提交；无须再次渲染原图。分类提交回执包含 `applied_changes` 和 `classification_counts`，可直接核对写入结果；提交后旧证据失效，结果复核可用空 evidence_ids 并在 notes 中保留此前原图结论。上述优化不绕过版本、原图证据及审计校验。正在运行的旧 MCP/桥接应用需要按本文的重启说明更新后才支持新参数；不要为了更新而中断未保存的识别草稿。

分类修正调用 `preview_stage_corrections(project_id, base_revision, stage=0, changes=[{"page_number": 3, "page_type": "Array Layout"}], evidence_ids=[...], reason="原图依据")`。合法类型为 `Overall Layout`、`Array Layout`、`Other`。检查预览后，用原有 `apply_corrections` 提交。分类修改会使下游结果失效。

**Agent2：** `agent2_review.extractions` 返回完整 Tracker 字段、模块列、各 Tracker 数量、INV 列表与逐行计数、warnings、OCR 文本及 raw；`derived_totals` 返回用于校验的结构。先根据已有字段、findings、warnings 和合计列出缺失、重复或不一致项，只为具体疑点查看原图。当前 Agent2 的 INV 行结构不等于 Agent3 的完整 Node 树。

修正时复制 `extractions` 的完整结构，修改对应字段，再调用 `preview_stage_corrections(stage=1, changes=<所有 Overall 页的完整 extraction 列表>, ...)`。预览返回重建后的 Tracker 定义；提交会清除旧 Agent3/4 结果。保留未修改页和字段，不能只传一个局部字段。Agent2 可能合并重复图纸，`raw.consolidated_page_numbers` 标识同组原页；保留该映射，并查看组内所有页的证据，避免重复统计。若合并判断错误，应依据原图拆分 extraction，而不能直接累加重复总数。

**每阶段完成结果审阅：** 调用 `confirm_stage_review(project_id, base_revision, stage=0或1或2, evidence_ids=[], notes="结果摘要与可疑点")` 即可继续，无须遍历原图。若确实看过图片，可提交有效 evidence_ids，记录会区分已看图页与结果审阅。真正修改识别内容仍要求对应页的有效原图证据。前序阶段未记录审阅时，下一阶段启动会被拒绝；记录保存至 `stage-reviews.json`。

**Agent3：** `list_review_issues` 和 context 中 `diagnostics` 即使没有最终结果也可读取；返回错误、最后步骤/页码/详情、并行任务、应识别页与缺少输出页。`last_failure` 保存失败瞬间的任务上下文，生产子进程异常包含 Python 堆栈，便于 Codex 定位代码。最后页和并行任务只是定位线索，不保证就是根因。`get_review_evidence` 可读取任意原 PDF 页，失败页也能检查。已有输出返回原图与叠加图；无输出页返回原图、完整渲染尺寸和现有分类/OCR 数据。

除 Tracker/Box 修正外，支持 `add_detection(kind="Node", name=..., inv_name=..., bbox=...)` 补漏检 Node。先提交 Node，再读取生成的 ID，为其补充 Tracker/Box。对完全无结果的 Layout 页，可先用 `initialize_page`，传 `page_number`、Agent2 中的 `inv_name`、原图证据元数据的 `image_width/image_height`，然后补 Node、表格和子元素。这些操作同样通过 `preview_corrections` → `apply_corrections`，重算几何并写审计。缺少任何 Layout 页输出时，不允许完成 Agent3 核验。

服务连接失败可以修复环境后重跑 `stage=2`。当前重跑是整个 Agent3 阶段，不是单页模型重试；进程崩溃前尚未返回的局部结果不保证保留。Codex 可以根据原图和本地脚本恢复草稿，但不能将未核实的页面补成空白后宣称识别成功。

更新后，关闭旧桥接应用并重启 Codex MCP 会话，再调用 `start_session`。打包版的 EXE 路径或文件名变化时，必须更新配置中的 `command`。若曾将 Skill 复制到个人目录，应同步更新该副本，或直接使用仓库内最新版 Skill。

## 状态与一致性

- 桌面应用是项目状态唯一持有者。MCP 是 STDIO 适配器，通过带随机令牌的 127.0.0.1 HTTP 桥接排队调用 Qt 主线程。
- 打包 EXE 默认开启桥接，每次普通双击创建独立窗口；MCP 可选择已有窗口或新建窗口。显式指定同一桥接文件时复用对应窗口。源码环境仍在 `--codex-bridge <discovery.json>` 下开启；调试方式见单独的源码开发接入说明。
- 用户手动上传和修改结果仍通过原 ViewModel。项目结果、分类或识别设置变化会使旧 revision/preview/evidence 失效；纯进度更新不会。识别与上传期间拒绝纠错写入。
- 只有几何输入或匹配表格串数发生变化的 Node 才重建几何；同页其他 Node 和仅修改 INV 名称时的 Row 结构保留。目标 Node 的几何编辑仍可能替换其手工 Row，必须查看预览。
- 修正预览和提交回执提供 `issue_changes`（消失、仍存、新增的问题记录）和 `repair_checks`（检测 Tracker 数、结构 Tracker 数、逐 Node 差异、校验状态与阻塞原因）。提交回执还提供 `preview_matches_published`，根据界面实际发布结果复核。问题记录消失不等于视觉正确；总数量接近不代表逐 Node 已正确。
- `review_complete` 仅表示阶段审阅已记录；`repair_complete` 要求结构/数量检查通过、无错误问题、方向已知且 INV 参考不冲突，仍不代表已验证所有图纸的视觉准确率。残留问题需按根因标记待处理，不能仅以提交次数或问题数下降宣称全部修复。
- Node 完整性独立检查：`table_node_not_detected` 表示表格中存在、已识别 Node 中未匹配的名称；`repair_checks.missing_node_candidates` 保留页码、候选名称和待确认状态。它是漏检/遮挡/名称或表格错误的候选，不直接证明实际缺少设备。确定位置和归属后用 `add_detection(kind="Node")` 补入，不能为了通过对账删除未确认的表格条目。
- 无法确定的遮挡节点可通过 `mark_node_for_review` 操作保存：传 page_number、name（可用临时描述）、reason，bbox 可选且仍为归一化全页坐标。不创建正式 Node、不计入数量；记录留在页面问题、修正审计和导出的 `repair_checks.manual_review_queue` 中。其他修改不会清除它；经人工或明确证据确认后，用 `resolve_node_review` 传 entity_id 和 reason 显式解除。以上操作沿用修正预览/提交与页面证据要求。存在待确认项时不宣称修复完成。
- 如果节点同时未出现在表格和检测结果里，仅靠表格对账无法发现；需结合具体区域的原图、未归属 Tracker/Box 或人工线索继续定位，不默认全图重查。
- `get_review_summary` 额外导出 `manual-review.json` 并返回 `manual_review_path`，包含 PDF 路径、页码、缺失 Node 候选及显式人工待确认项，便于后续人工接手。候选项不计入正式设备数量；问题列表与导出汇总都会反映这些未解决项。
- 修正操作不直接修改生产 Count 项目；先更新 AI Statistics 的识别草稿。原有确认/校验/导入 Count 流程继续可用。
- 每次提交保存 before/after 审计 JSON，界面通过 `replace_element_result` 的 state_changed 信号立即刷新。数据保存在各窗口桥接文件同级的 `projects/<project_id>` 目录；应用关闭后仍保留。源码运行目录 `.codex-runtime` 已加入 Git 忽略。
- request_id 在当前应用会话内幂等。重试完全相同的请求返回原回执；同 ID 不同请求会被拒绝。

## 坐标与证据

工具输入框、图片工具的 detections/issues 框：页码从 1 开始，左上原点，xyxy 归一化到完整渲染页面（遵循 PDF 页面旋转），不包含 GUI 留白。截图缩放或裁剪不改变该坐标定义；bbox 仍相对于完整页面。

完整 `result.json`、审计 before/after、table_bbox/table_bboxes 和 trace 中的 bbox 保留内部像素坐标，使用对应 page 的 image_width/image_height 转换。证据包含原始 PDF 路径、无图像增强的原图哈希，以及原图/叠加图本地文件；PDF 改动后应重新导入并识别。

## 当前边界

- 支持分类及完整 Agent2 数据修正、失败 Layout 页恢复、Node 新建、Tracker/Box 补删、检测框移动/归属、Node 改名、整页表格行替换。`delete_node` 仅允许删除已无子设备的 Node，需先转移或删除子设备；`rename_inv` 支持仅属于指定页的 INV，跨页合并分支会被拒绝。尚不支持自动回滚工具。
- 纠错重建按 INV 分支身份保留节点，不使用可能重复或为空的名称作为唯一键。预览还会校验原有页面及未显式删除的节点未丢失。
- 使用生产几何算法，Tracker 类型分配仍以当前串数与长度约束为依据。矛盾不会通过人为凑数解决；不确定情况继续留待复核。
- jobs、preview、evidence 索引和幂等回执驻留应用内存；应用重启后的自动恢复留给后续阶段。审计完整快照仍会保存。
- MCP 不能自行唤醒已经结束的 Codex 对话。当前对话查询任务并完成复核；应用中的识别任务可继续运行。
- 每阶段记录已有结果的审阅结论后即可继续，不强制看图；审阅状态不跨应用重启自动恢复。只有实际修改才强制要求原图证据。

## 开发验证

以下命令仅供源码开发环境使用；打包版接入按前文第 5 步验证。

```powershell
uv run --extra mcp pytest tests/test_recognition_queue.py tests/test_agent3_process_cancellation.py tests/test_packaged_mcp_runtime.py tests/test_codex_integration.py tests/test_ai_statistics_tab.py tests/test_main_window_controllers.py
```

测试使用生成的 PDF 和离线识别草稿，验证真实几何重算、Agent4 数量、原图/叠加图、Qt 界面同步、本地 HTTP 调用、MCP 工具 schema 与原生图片、非法操作原子拒绝、版本冲突和幂等提交。真实模型/远程服务准确率还需要用实际项目 PDF 另外验收。

## 参考资料

- [Codex 官方 MCP 接入文档](https://learn.chatgpt.com/docs/extend/mcp)：本地配置位置、STDIO 启动参数和 CLI 注册方式。
- [打包版 MCP 配置示例](../integrations/codex/mcp-config.packaged.example.toml)。
- [源码开发接入说明](CODEX_SOURCE_DEVELOPMENT.md)。
- [本项目自动复核 Skill](../integrations/codex/skills/count-design-review/SKILL.md)。
