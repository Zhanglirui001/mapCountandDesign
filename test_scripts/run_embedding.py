from dataclasses import asdict
import json

from count_and_design.application.ai import (
    EmbeddingClient,
    DRAWING_CLASSIFICATION_PROMPT,
)

client = EmbeddingClient.from_env(".env")

# 查看模型探测结果
print("模型列表：", client.list_models())
print("使用模型：", client.resolve_model())

# 单张图片
result = client.embed_image(
    r"image.png",
    prompt=DRAWING_CLASSIFICATION_PROMPT,
)

print(json.dumps(asdict(result), ensure_ascii=False, indent=2))

results = client.embed_images(
    ["image1.png", "image2.png"],
    prompt=DRAWING_CLASSIFICATION_PROMPT,
    max_concurrent=8,
    on_progress=lambda completed, total: print(f"{completed}/{total}"),
)

print(json.dumps(
    [asdict(result) for result in results],
    ensure_ascii=False,
    indent=2,
))
