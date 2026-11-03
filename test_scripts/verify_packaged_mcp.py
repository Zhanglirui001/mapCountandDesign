"""Offline EXE acceptance: real STDIO and independent project windows.

Run with the development Python: python test_scripts/verify_packaged_mcp.py <exe>.
Only the harness imports source; child processes run the EXE with no source path.
Recognition concurrency and cancellation are covered by deterministic pytest
workers. This harness does not call paid OCR/LLM providers.
"""

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from uuid import uuid4



def write_pdf(path, page_count):
    page_ids = [3 + i * 2 for i in range(page_count)]
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode()]
    for page_id in page_ids:
        objects += [f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Contents {page_id + 1} 0 R >>".encode(),
                    b"<< /Length 0 >>\nstream\n\nendstream"]
    data, offsets = b"%PDF-1.4\n", [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    data += f"trailer\n<< /Root 1 0 R /Size {len(offsets)} >>\nstartxref\n{xref}\n%%EOF".encode()
    path.write_bytes(data)


def main():
    exe = Path(sys.argv[1]).resolve()
    work = Path("build").resolve() / ("multiwindow-smoke-" + uuid4().hex[:8])
    work.mkdir(parents=True)
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "COUNT_DESIGN_PROJECT_ROOT", "COUNT_DESIGN_BRIDGE_FILE"):
        env.pop(key, None)
    env.update(LOCALAPPDATA=str(work / "local"), APPDATA=str(work / "roaming"),
               COUNT_DESIGN_QUEUE_DIR=str(work / "queue"), QT_QPA_PLATFORM="offscreen")
    owned = []
    print(f"Evidence directory: {work}", flush=True)
    with (work / "stderr.log").open("w", encoding="utf-8") as log:
        try:
            # Equivalent to double-click: no arguments, no inherited source cwd.
            gui = subprocess.Popen([str(exe)], cwd=work, env=env, stdout=log, stderr=log)
            owned.append(gui)
            windows = work / "local" / "CountAndDesign" / "runtime" / "windows"
            deadline = time.monotonic() + 120
            while not list(windows.glob("*/bridge.json")):
                assert gui.poll() is None, "Double-click GUI exited before bridge startup"
                assert time.monotonic() < deadline, "GUI startup timeout"
                time.sleep(.2)
            process = subprocess.Popen([str(exe), "--mcp"], cwd=work, env=env,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
            owned.append(process)
            messages = queue.Queue()
            def reader():
                for line in process.stdout:
                    messages.put(line)
                messages.put(None)
            threading.Thread(target=reader, daemon=True).start()
            def send(payload):
                process.stdin.write(json.dumps(payload).encode() + b"\n")
                process.stdin.flush()
            counter = 0
            def request(method, params):
                nonlocal counter
                counter += 1
                send(dict(jsonrpc="2.0", id=counter, method=method, params=params))
                while True:
                    raw = messages.get(timeout=150)
                    assert raw is not None, f"MCP exited; inspect {work / 'stderr.log'}"
                    response = json.loads(raw)
                    if response.get("id") == counter:
                        assert "error" not in response, response
                        return response["result"]
            initialized = request("initialize", dict(protocolVersion="2024-11-05", capabilities={},
                                                      clientInfo=dict(name="multiwindow-smoke", version="1")))
            assert "one running Agent3 across all windows" in initialized["instructions"]
            send(dict(jsonrpc="2.0", method="notifications/initialized"))
            assert len(request("tools/list", {})["tools"]) == 14
            def call(name, **arguments):
                result = request("tools/call", dict(name=name, arguments=arguments))
                assert not result.get("isError"), result
                return json.loads(next(c["text"] for c in result["content"] if c["type"] == "text"))
            a = call("start_session")
            assert len(list(windows.glob("*/bridge.json"))) == 1, "Did not reuse double-clicked window"
            pdf_a, pdf_b = work / "A.pdf", work / "B.pdf"
            write_pdf(pdf_a, 1)
            write_pdf(pdf_b, 2)
            imported_a = call("import_pdf", path=str(pdf_a))
            # A loaded but unrecognized PDF must survive an accidental import.
            blocked = request("tools/call", dict(name="import_pdf", arguments=dict(path=str(pdf_b))))
            assert blocked.get("isError"), blocked
            assert "new_window=True" in str(blocked), blocked
            preserved = call("get_job_status")
            assert preserved["project_id"] == imported_a["project_id"]
            assert preserved["pdf_path"] == str(pdf_a) and preserved["page_count"] == 1
            b = call("start_session", new_window=True)
            assert b["window_id"] != a["window_id"] and b["page_count"] == 0
            imported_b = call("import_pdf", path=str(pdf_b))
            assert len(call("list_sessions")["sessions"]) == 2
            selected_a = call("start_session", window_id=a["window_id"])
            assert selected_a["project_id"] == imported_a["project_id"] and selected_a["page_count"] == 1
            selected_b = call("start_session", window_id=b["window_id"])
            assert selected_b["project_id"] == imported_b["project_id"] and selected_b["page_count"] == 2
            # A second ordinary GUI launch must create a third independent window.
            another_gui = subprocess.Popen([str(exe)], cwd=work, env=env, stdout=log, stderr=log)
            owned.append(another_gui)
            deadline = time.monotonic() + 120
            while len(list(windows.glob("*/bridge.json"))) < 3:
                assert another_gui.poll() is None
                assert time.monotonic() < deadline
                time.sleep(.2)
            assert len(call("list_sessions")["sessions"]) == 3
            print(json.dumps(dict(tools=14, independent_windows=3, pdf_pages=[1, 2],
                                  selection_preserved=True,
                                  unprocessed_pdf_protected=True,
                                  no_source_environment=True)), flush=True)
        finally:
            for child in reversed(owned):
                if child.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                child.wait(timeout=20)


if __name__ == "__main__":
    main()
