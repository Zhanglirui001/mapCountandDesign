# Codex MCP 源码开发接入

本文仅适用于维护源码、调试 Python 服务或重新打包。使用桌面 EXE 时，按 [打包版接入说明](CODEX_INTEGRATION.md) 操作，无需执行本页命令。

## 安装开发依赖

```powershell
Set-Location 'C:\Data\2D_design\count-and-design'
uv sync --extra mcp --dev
```

## 源码模式配置

开发者可使用 [源码 MCP 配置示例](../integrations/codex/mcp-config.example.toml)。该配置通过项目虚拟环境启动 Python 模块，依赖源码目录和 Python 环境。将其中路径改为实际开发目录。

在 Codex 中选择源码配置或打包版配置之一，替换已有的同名 MCP 表，避免重复定义。从打包版切换到源码版后，保存配置、重启 Codex，并新开会话。

源码模式的 `start_session` 会启动带桥接的 Python GUI。普通 `start.bat` 打开的窗口没有桥接；如需手动调试桥接，运行：

```powershell
uv run python -m count_and_design --codex-bridge .codex-runtime/bridge.json
```

源码模式的默认桥接文件为项目目录下的 `.codex-runtime/bridge.json`，自动启动日志为 `.codex-runtime/app.log`，复核数据保存在 `.codex-runtime/projects`。

## 测试与打包

```powershell
uv run --extra mcp pytest tests/test_packaged_mcp_runtime.py tests/test_codex_integration.py
uv run pyinstaller count_and_design.spec --clean
```

安装依赖时必须包含 `mcp` extra。打包后的同一个 EXE 支持直接打开 GUI，也支持 `--mcp` STDIO 入口。分发后使用打包版配置，不能继续指向开发机的 Python 路径。

Windows 无控制台打包会恢复 MCP 客户端继承的标准输入输出管道。打包规则还会优先使用当前 Python 自带的 OpenSSL DLL，避免误收集 PATH 中同名的 Conda 库。

打包验收需额外检查：从不含源码的目录运行 EXE、MCP 握手和工具列表、自动启动 GUI、连接已双击打开的 GUI、PDF 导入与项目重连，以及重复启动时的窗口复用。离线测试不代表真实 OCR/LLM 服务准确率已通过验收。
