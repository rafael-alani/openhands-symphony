#!/usr/bin/env python3
"""Run as openhands-agent: verify ACP settings without starting model turns."""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import tempfile
import threading
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", nargs=argparse.REMAINDER,
                        help="probe this installed ACP command directly, without a shell or copied adapter")
    args = parser.parse_args()
    if args.command == []:
        parser.error("--command requires an executable")
    if os.geteuid() == 0:
        raise SystemExit("Run this probe as the authenticated openhands-agent worker, not root")
    package = Path("/opt/openhands-acp/node_modules/@agentclientprotocol/codex-acp")
    with tempfile.TemporaryDirectory(prefix="symphony-acp-settings-") as temporary:
        root = Path(temporary)
        command = args.command
        if command is None:
            from patch_codex_acp import patch

            copied = root / "adapter"
            (copied / "dist").mkdir(parents=True)
            (copied / "package.json").write_bytes((package / "package.json").read_bytes())
            (copied / "dist/index.js").write_bytes((package / "dist/index.js").read_bytes())
            patch(copied)
            command = ["node", str(copied / "dist/index.js")]
        probe_settings(command, root)


def probe_settings(command: list[str], root: Path) -> None:
    for effort, speed in (("xhigh", "normal"), ("low", "fast")):
        home = root / speed
        home.mkdir()
        environment = {**os.environ,
                       "CODEX_PATH": "/opt/provider-clis/node_modules/.bin/codex",
                       "CODEX_CONFIG": json.dumps({"model_reasoning_effort": effort,
                                                   "service_tier": "fast" if speed == "fast" else None})}
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "DEFAULT_AUTH_REQUEST", "GH_TOKEN", "GITHUB_TOKEN"):
            environment.pop(key, None)
        process = subprocess.Popen(command, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        responses = queue.Queue()

        def read(process=process, responses=responses):
            try:
                for line in process.stdout:
                    responses.put(json.loads(line))
            except ValueError:
                responses.put({"probe_error": "ACP returned invalid JSON"})
            finally:
                responses.put({"probe_error": "ACP exited before answering the settings probe"})

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        def request(identifier, method, params, process=process, responses=responses):
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identifier,
                                           "method": method, "params": params}) + "\n")
            process.stdin.flush()
            for _ in range(100):
                response = responses.get(timeout=30)
                if "probe_error" in response:
                    raise RuntimeError(response["probe_error"])
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
            if effort not in result.get("models", {}).get("currentModelId", ""):
                raise RuntimeError("ACP did not apply the requested reasoning effort")
            if options.get("fast-mode", {}).get("currentValue") != ("on" if speed == "fast" else "off"):
                raise RuntimeError("ACP did not apply the requested speed")
        finally:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
            reader.join(timeout=2)
            process.stdout.close()


if __name__ == "__main__":
    main()
