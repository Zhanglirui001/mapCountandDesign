# Agent2 Overall Site 识别 — 视觉模型与策略

Agent2（Info Recognition）负责 overall-site 图纸页的表格读取，产出 tracker 字段与
INV 串数汇总，供 Agent4 校验使用。

核心代码：`src/count_and_design/application/ai_statistics/info_recognition/overall_site.py`，
入口 `Agent2OverallSiteRecognizer.recognize_page()`。

两阶段彼此独立：**定位**（YOLO / MinerU）与**抽取**（视觉模型读表）。

## 策略开关

`AGENT2_OVERALL_TABLE_STRATEGY` 同时决定定位方式与视觉提供方：

| 策略 | 定位 | 视觉抽取 |
|------|------|----------|
| `yolo_glm`（默认） | YOLO 检测 + MinerU 兜底 | GLM-5.3-Flash（OpenRouter） |
| `mineru_glm` | MinerU layout | GLM-5.3-Flash（OpenRouter） |
| `yolo_claude` | YOLO 检测 + MinerU 兜底 | Claude（Bedrock） |
| `mineru_claude` | MinerU layout | Claude（Bedrock） |

命名规则统一为 `<定位>_<提供方>`：前缀 `yolo`/`mineru` 决定定位，后缀 `glm`/`claude`
决定视觉提供方。`OverallSiteExtractionConfig.uses_glm` 由后缀推导，因此新增策略无需改动
抽取代码。

```env
AGENT2_OVERALL_TABLE_STRATEGY=yolo_glm
# *_glm 使用；未设置时回落到 OPENROUTER_MODEL
# AGENT2_OVERALL_TABLE_OPENROUTER_MODEL=z-ai/glm-5.3-flash
AGENT2_BEDROCK_MAX_TOKENS=12000
```

## 提供方选择

`Agent2OverallSiteRecognizer._vision_llm()` 按配置返回提供方：

- `*_glm` → 注入的 `glm`（测试用）或 `OpenRouterLlmProvider`，模型取
  `AGENT2_OVERALL_TABLE_OPENROUTER_MODEL` → `OPENROUTER_MODEL` → `z-ai/glm-5.3-flash`
- `*_claude` → 注入的 `bedrock` 或 `BedrockLlmProvider`

`bedrock` / `glm` 都是可选注入参数，未注入时按策略惰性构造，因此不会为未使用的
提供方建立客户端。`raw.vision_provider` 记录实际使用的提供方（`openrouter` / `bedrock`），
错误信息使用 `vision_provider_label`（`GLM` / `Claude`）。

所有抽取入口（`_extract`、`_extract_batch`，以及 tracker 列补抽与 INV 表恢复）都经由
`_vision_llm()`，因此一次运行内提供方始终一致。

## 重试策略保持提供方

`AIStatisticsWorkflow._configure_overall_site_recognizer(retry=...)` 在重试时只把**定位**
从 YOLO 换成 MinerU，**不改变视觉提供方**：`*_glm` 重试为 `mineru_glm`，`*_claude`
重试为 `mineru_claude`。

## 输出预算（重要）

`AGENT2_BEDROCK_MAX_TOKENS` 是**所选提供方**输出上限，Agent2 显式传入
`max_completion_tokens`，会覆盖 `OPENROUTER_MAX_TOKENS` / `BEDROCK_MAX_TOKENS`。

GLM-5.3-Flash 是推理模型：`max_completion_tokens` **同时包含 reasoning token 与最终
JSON**。实测读取一张 2886×612 的 DC Cable Schedule 裁剪图：

| 预算 | reasoning | 最终 content | finish_reason |
|------|-----------|--------------|---------------|
| 256 | 255 | 0 字节（空） | `length` |
| 2048 | 208 | 563 字节合法 JSON | `stop` |

即预算过小时 GLM 会先耗尽 token 推理，返回**空 content**，Agent2 会判定为 JSON 解析
失败并触发一次纠正重试，两次都失败则该页识别失败。Agent2 的完整 payload（tracker
字段 + 逐 INV 行）明显大于上面的探针，建议保留充足余量（12000 起步），不要沿用给
非推理模型设定的小上限。

## 测试

- `tests/test_agent2_overall_site.py` — 策略/提供方选择、GLM 抽取路径、配置回落
- `tests/test_ai_statistics_workflow.py` — 重试保持提供方
