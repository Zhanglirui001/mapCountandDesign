# Count and Design

Count and Design 是基于 Python 和 PySide6 的电气项目统计与设计桌面应用，提供 AI 图纸统计、项目数据编辑、产品生成、压降报告、数据填充与版本历史等功能。

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Windows 为主要开发和发布平台

## 快速开始

Windows：

```powershell
.\start.bat
```

Linux / macOS：

```bash
./start.sh
```

手动安装与启动：

```powershell
uv sync --dev
uv run python -m count_and_design
```

## AI 服务配置

需要使用 OCR、LLM 或 Embedding 服务时，复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

根据开发环境填写 `.env`。真实服务地址、API Key 和 Token 不得提交到版本库。

## 测试

全量测试使用 pytest-xdist 按文件分发（每个文件固定在一个 worker，避免拆分模块级 fixture）：

```powershell
uv run pytest -n auto --dist loadfile
```

worker 数量默认按逻辑核数自动确定；核数很多时每个 worker 的模块导入开销会摊薄收益，可用 `-n 4` 之类的手动指定。单文件或调试时去掉 `-n auto`（或加 `-n0`）：

```powershell
uv run pytest tests/test_application_workflows.py
```

## 项目结构

```text
src/count_and_design/
├─ models/          # 领域模型
├─ application/     # 工作流、服务、算法和 AI 适配
├─ gui/             # Views、ViewModels 和 Controllers
├─ i18n/            # 国际化资源
└─ resources/       # 应用资源和本地模型

tests/              # 自动测试
test_scripts/       # AI 试验、诊断与性能脚本
docs/               # 开发文档
```

依赖方向：

```text
GUI Views / Controllers
        ↓
ViewModels
        ↓
Application Session / Workflows / Services
        ↓
Domain Models
```

## 分支与发布

- `master`：发布主干，触发自动版本管理；
- `dev`：集成分支，生成开发预览包；
- `feature/<topic>`：新功能；
- `fix/<topic>`：缺陷修复；
- `refactor/<topic>`：重构；
- `docs/<topic>`：文档。

提交前至少执行：

```powershell
uv sync --frozen
uv run pytest
git diff --check
```

## 文档

- [Agent1 页面分类说明](docs/AGENT1_PAGE_CLASSIFICATION.md)
- [Codex 工具接入与自动复核](docs/CODEX_INTEGRATION.md)
- [Agent2 Overall Site 视觉模型与策略](docs/AGENT2_OVERALL_SITE_VISION.md)
- [Agent3 并发说明](docs/AGENT3_CONCURRENCY.md)
- [Block Diagram 表格流程](docs/BLOCK_DIAGRAM_TABLE_FLOW.md)
- [数据类型说明](DATA_TYPES.md)
- [版本变更记录](CHANGELOG.md)
- [发布说明](RELEASE_NOTES.md)

## 打包

```powershell
uv sync --extra mcp --dev
uv run pyinstaller count_and_design.spec --clean
```

打包后的 EXE 支持双击启动界面，以及通过 `--mcp` 启动内置 MCP 服务。
Codex 可连接已打开的窗口，也可自动启动应用，无需源码或 Python 环境。
配置示例见 [打包版 MCP 配置](integrations/codex/mcp-config.packaged.example.toml)。

正式发布由 GitLab CI 在 Windows Runner 上完成。
