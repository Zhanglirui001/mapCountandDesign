"""Image preparation helpers shared by AI workflow stages."""

from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image


class ImageTransportError(RuntimeError):
    """Raised when an image cannot fit a provider's transport limit."""


def bedrock_image_transport_size(image_bytes: bytes) -> int:
    """Return the base64-encoded size enforced by Bedrock."""

    return len(base64.b64encode(image_bytes))


def compress_image_for_bedrock(
    image_bytes: bytes,
    *,
    max_transport_bytes: int,
) -> tuple[bytes, str]:
    """Compress an image until its encoded payload fits Bedrock's limit."""

    if bedrock_image_transport_size(image_bytes) <= max_transport_bytes:
        return image_bytes, "image/png"
    with Image.open(BytesIO(image_bytes)) as image:
        rgb = image.convert("RGB")
        for scale in (1.0, 0.85, 0.7, 0.55, 0.4):
            resized = rgb
            if scale < 1.0:
                resized = rgb.resize(
                    (
                        max(1, int(rgb.width * scale)),
                        max(1, int(rgb.height * scale)),
                    ),
                    Image.Resampling.LANCZOS,
                )
            for quality in (85, 70, 55, 40, 30):
                buffer = BytesIO()
                resized.save(
                    buffer,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                )
                compressed = buffer.getvalue()
                if (
                    bedrock_image_transport_size(compressed)
                    <= max_transport_bytes
                ):
                    return compressed, "image/jpeg"
    raise ImageTransportError(
        "Image exceeds the Bedrock transport limit even after compression "
        f"({len(image_bytes)} bytes raw, "
        f"{bedrock_image_transport_size(image_bytes)} bytes encoded, "
        f"limit {max_transport_bytes} bytes encoded)."
    )
