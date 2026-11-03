"""Entry point for the GUI, packaged Agent3 runtime smoke test, and frozen executable."""

from __future__ import annotations

import sys


def _run_agent3_model_self_test() -> None:
    """Load and execute both bundled models inside the active runtime."""

    from io import BytesIO

    from PIL import Image

    from count_and_design.application.ai import (
        BOX_MODEL_TYPE,
        TRACKER_MODEL_TYPE,
        YoloClient,
    )

    output = BytesIO()
    Image.new("RGB", (640, 640), "white").save(output, format="PNG")
    client = YoloClient.from_defaults(device="cpu")
    for model_type in (TRACKER_MODEL_TYPE, BOX_MODEL_TYPE):
        client.detect(output.getvalue(), model_type=model_type)


if __name__ == "__main__":
    if "--agent3-model-self-test" in sys.argv:
        _run_agent3_model_self_test()
    else:
        import multiprocessing

        # PyInstaller must divert spawned ProcessPool workers before importing
        # PySide6 and constructing any GUI objects.
        multiprocessing.freeze_support()

        if "--mcp" in sys.argv:
            from count_and_design.integration.runtime import prepare_stdio
            prepare_stdio()
            from count_and_design.integration.mcp_server import main
        else:
            # Windowed builds have no console; libraries may still print logs.
            import os
            for name in ("stdin", "stdout", "stderr"):
                if getattr(sys, name) is None:
                    setattr(sys, name, open(os.devnull, "r" if name == "stdin" else "w"))
            from count_and_design.main import main

        sys.exit(main())
