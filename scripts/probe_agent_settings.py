#!/usr/bin/env python3
"""Run as openhands-agent: verify ACP settings without starting model turns."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
from pathlib import Path

from patch_codex_acp import patch


def main() -> None:
    if os.geteuid() == 0:
        raise SystemExit("Run this probe as the authenticated openhands-agent worker, not root")
    package = Path("/opt/openhands-acp/node_modules/@agentclientprotocol/codex-acp")
    with tempfile.TemporaryDirectory(prefix="symphony-acp-settings-") as temporary:
        root = Path(temporary)
        copied = root / "adapter"
        (copied / "dist").mkdir(parents=True)
        (copied / "package.json").write_bytes((package / "package.json").read_bytes())
        (copied / "dist/index.js").write_bytes((package / "dist/index.js").read_bytes())
        patch(copied)
        for effort, speed in (("xhigh", "normal"), ("low", "fast")):
            home = root / speed
            home.mkdir()
            environment = {**os.environ,
                           "CODEX_PATH": "/opt/provider-clis/node_modules/.bin/codex",
                           "CODEX_CONFIG": json.dumps({"model_reasoning_effort": effort,
                                                       "service_tier": "fast" if speed == "fast" else None})}
            for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "DEFAULT_AUTH_REQUEST", "GH_TOKEN", "GITHUB_TOKEN"):
                environment.pop(key, None)
            process = subprocess.Popen(["node", str(copied / "dist/index.js")], env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            responses = queue.Queue()

            def read(process=process, responses=responses):
                for line in process.stdout:
                    responses.put(json.loads(line))

            threading.Thread(target=read, daemon=True).start()

            def request(identifier, method, params, process=process, responses=responses):
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identifier,
                                               "method": method, "params": params}) + "\n")
                process.stdin.flush()
                for _ in range(100):
                    response = responses.get(timeout=30)
                    if response.get("id") == identifier:
                        if "error" in response:
                            raise RuntimeError(json.dumps(response["error"]))
                        return response["result"]
                raise RuntimeError("too many notifications")

            try:
                request(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                         "clientInfo": {"name": "symphony-settings-probe", "version": "1"}})
                result = request(2, "session/new", {"cwd": str(home), "mcpServers": []})
                options = {option["id"]: option for option in result.get("configOptions", [])}
                print(json.dumps({"requested_effort": effort, "requested_speed": speed,
                                  "model": result.get("models", {}).get("currentModelId"),
                                  "fast_mode": options.get("fast-mode", {}).get("currentValue")},
                                 sort_keys=True), flush=True)
                assert effort in result["models"]["currentModelId"]
                assert options["fast-mode"]["currentValue"] == ("on" if speed == "fast" else "off")
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
