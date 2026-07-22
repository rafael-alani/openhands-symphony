from __future__ import annotations

import grp
import json
import os
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Config
from .coordinator import Coordinator
from .store import Store
from .workspace import SENSITIVE_ENV, validation_argv


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _command_check(name: str, command: str, required: bool = True) -> Check:
    path = shutil.which(command)
    return Check(name, path is not None, path or "not installed", required)


def _run(command: list[str], *, environment: dict[str, str] | None = None) -> tuple[int, str]:
    if environment is None:
        environment = os.environ.copy()
    if command and command[0] == "gh":
        environment.setdefault("GH_CONFIG_DIR", "/var/lib/openhands-symphony/github")
    try:
        process = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
            env=environment,
        )
        return process.returncode, process.stdout.strip()[-2000:]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def _version_manifest() -> dict[str, str]:
    candidates = [
        Path("/opt/openhands-symphony/versions.env"),
        Path(__file__).resolve().parents[2] / "versions.env",
    ]
    for path in candidates:
        if path.is_file():
            values: dict[str, str] = {}
            for line in path.read_text().splitlines():
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
            return values
    return {}


def _exact_version(name: str, command: list[str], expected: str) -> Check:
    environment = os.environ.copy()
    environment["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    status, output = _run(command, environment=environment)
    tokens = re.findall(r"\d+\.\d+\.\d+", output)
    observed = tokens[0] if tokens else output
    return Check(name, status == 0 and observed == expected, f"expected {expected}; observed {observed or 'none'}")


def _github_version(values: dict[str, str]) -> Check:
    status, output = _run(["gh", "--version"])
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", output)
    if status or not match:
        return Check("GitHub CLI version", False, output or "unavailable")
    observed = tuple(int(value) for value in match.groups())
    minimum = tuple(int(value) for value in values.get("GH_MIN_VERSION", "2.40.0").split("."))
    maximum_major = int(values.get("GH_MAX_MAJOR", "2"))
    return Check(
        "GitHub CLI version",
        observed >= minimum and observed[0] <= maximum_major,
        f"supported >= {'.'.join(map(str, minimum))}, major <= {maximum_major}; observed {'.'.join(match.groups())}",
    )


def _service_accounts() -> Check:
    try:
        orchestrator = pwd.getpwnam("openhands-symphony")
        worker = pwd.getpwnam("openhands-agent")
        validator = pwd.getpwnam("openhands-validator")
        shared = grp.getgrnam("openhands-agents")
        operators = grp.getgrnam("openhands-operators")
    except KeyError as exc:
        return Check("service account isolation", False, f"missing account or group: {exc}")
    separate = len({orchestrator.pw_uid, worker.pw_uid, validator.pw_uid}) == 3
    members = set(shared.gr_mem)
    shared_ok = (
        (orchestrator.pw_name in members or orchestrator.pw_gid == shared.gr_gid)
        and (worker.pw_name in members or worker.pw_gid == shared.gr_gid)
        and (validator.pw_name in members or validator.pw_gid == shared.gr_gid)
    )
    operator_members = set(operators.gr_mem)
    operators_ok = orchestrator.pw_name in operator_members or orchestrator.pw_gid == operators.gr_gid
    worker_excluded = worker.pw_name not in operator_members and worker.pw_gid != operators.gr_gid
    validator_excluded = validator.pw_name not in operator_members and validator.pw_gid != operators.gr_gid
    return Check(
        "service account isolation",
        separate and shared_ok and operators_ok and worker_excluded and validator_excluded,
        f"orchestrator uid={orchestrator.pw_uid}, worker uid={worker.pw_uid}, "
        f"validator uid={validator.pw_uid}, shared_group={shared.gr_gid}, operators_group={operators.gr_gid}",
    )


def _canvas_key_permissions(config: Config) -> Check:
    try:
        info = config.service.agent_server_api_key_file.stat()
        group = grp.getgrgid(info.st_gid).gr_name
        mode = stat.S_IMODE(info.st_mode)
    except (OSError, KeyError) as exc:
        return Check("Canvas key file boundary", False, str(exc))
    return Check(
        "Canvas key file boundary",
        info.st_uid == 0 and group == "openhands-symphony" and mode == 0o640,
        f"path={config.service.agent_server_api_key_file}, owner_uid={info.st_uid}, group={group}, mode={oct(mode)}",
    )


def _validator_boundary(config: Config) -> Check:
    if not config.service.validation_user:
        return Check("credential-free validation boundary", False, "service.validation_user is disabled")
    user = config.service.validation_user
    expected_home = f"/var/lib/{user}"
    expected_path = "/opt/browser-use/bin:/usr/local/bin:/usr/bin:/bin"
    probe = (
        "/bin/sh",
        "-c",
        'printf "user=%s\\nhome=%s\\npath=%s\\nci=%s\\numask=%s\\n" '
        '"$(id -un)" "$HOME" "$PATH" "$CI" "$(umask)"; /usr/bin/env',
    )
    status, output = _run(validation_argv(probe, user))
    expected = {
        f"user={user}",
        f"home={expected_home}",
        f"path={expected_path}",
        "ci=true",
        "umask=0007",
    }
    output_lines = output.splitlines()
    sensitive_exposed = any(line.startswith(f"{name}=") for name in SENSITIVE_ENV for line in output_lines)
    ok = status == 0 and expected.issubset(set(output_lines)) and not sensitive_exposed
    return Check(
        "credential-free validation boundary",
        ok,
        (
            f"exact setup/validation launch succeeded as {user}; clean environment; umask=0007"
            if ok
            else output or f"exact setup/validation launch failed with exit {status}"
        ),
    )


def _workspace_permissions(config: Config) -> Check:
    try:
        mode = config.service.workspace_dir.stat().st_mode
        group = grp.getgrgid(config.service.workspace_dir.stat().st_gid).gr_name
    except (OSError, KeyError) as exc:
        return Check("workspace confinement", False, str(exc))
    return Check(
        "workspace confinement",
        stat.S_ISDIR(mode) and bool(mode & stat.S_ISGID) and group == "openhands-agents",
        f"path={config.service.workspace_dir}, group={group}, mode={oct(stat.S_IMODE(mode))}",
    )


def _empty_setup_behavior(config: Config, coordinator: Coordinator) -> Check:
    try:
        result = coordinator.workspaces.run_setup(
            config.service.workspace_dir,
            "",
            config.service.validation_user,
        )
    except Exception as exc:
        return Check("empty repository setup", False, str(exc))
    return Check(
        "empty repository setup",
        result is None,
        "setup_script='' is a no-op" if result is None else "empty setup unexpectedly executed a command",
    )


def _credential_exposure(path: Path) -> tuple[bool, str]:
    try:
        path.stat()
    except FileNotFoundError:
        return False, f"{path} is absent"
    except PermissionError:
        return False, f"{path} is not visible to the orchestrator (expected account isolation)"
    except OSError as exc:
        return True, f"could not verify {path}: {exc}"
    return True, f"{path} is visible to the orchestrator"


def _agent_server(config: Config, expected: str) -> Check:
    headers: dict[str, str] = {}
    try:
        if config.service.agent_server_api_key_file.is_file():
            key = config.service.agent_server_api_key_file.read_text().strip()
            if key.startswith("LOCAL_BACKEND_API_KEY="):
                key = key.split("=", 1)[1]
            headers["X-Session-API-Key"] = key
        response = httpx.get(f"{config.service.agent_server_url}/server_info", headers=headers, timeout=15)
        response.raise_for_status()
        payload = response.json()
        observed = str(payload.get("version") or payload.get("agent_server_version") or "unknown")
        return Check(
            "OpenHands Agent Server version", observed == expected, f"expected {expected}; observed {observed}"
        )
    except (OSError, ValueError, httpx.HTTPError) as exc:
        return Check("OpenHands Agent Server version", False, str(exc))


def _browser_cdp(expected: str) -> Check:
    try:
        response = httpx.get("http://127.0.0.1:9222/json/version", timeout=10)
        response.raise_for_status()
        product = str(response.json().get("Browser") or "")
        observed = product.rsplit("/", 1)[-1] if "/" in product else product
        return Check(
            "headless Chromium CDP", observed == expected, f"expected {expected}; observed {observed or 'none'}"
        )
    except (ValueError, httpx.HTTPError) as exc:
        return Check("headless Chromium CDP", False, str(exc))


def _service_failure_detail(service: str, state: str) -> str:
    if state == "active":
        return state
    status, output = _run(["journalctl", "-u", service, "--no-pager", "-n", "8"])
    if status != 0 and ("permission" in output.lower() or "no journal files were opened" in output.lower()):
        return (
            f"state={state or 'unknown'}; service account cannot read the system journal; run: "
            f"sudo journalctl -u {service} -b -n 100 --no-pager -o cat"
        )
    if output:
        return f"state={state or 'unknown'}; recent journal: {output}"
    return f"state={state or 'unknown'}; journal unavailable (exit {status})"


def run_doctor(config: Config, store: Store, coordinator: Coordinator) -> list[Check]:
    versions = _version_manifest()
    keyring_status, keyring_output = _run(["systemctl", "is-active", "openhands-agent-keyring.service"])
    guest_agent_status, guest_agent_output = _run(["systemctl", "is-active", "qemu-guest-agent.service"])
    browser_status, browser_output = _run(["systemctl", "is-active", "openhands-browser.service"])
    firewall_status, firewall_output = _run(["systemctl", "is-active", "openhands-symphony-firewall.service"])
    nft_status, nft_output = _run(["sudo", "-n", "/usr/sbin/nft", "list", "table", "inet", "openhands_symphony"])
    canvas_environment_status, canvas_environment = _run(
        ["systemctl", "show", "openhands-canvas.service", "--property=Environment"]
    )
    browser_environment_status, browser_environment = _run(
        ["systemctl", "show", "openhands-browser.service", "--property=Environment"]
    )
    browser_state_values = (
        "SYMPHONY_BROWSER_PROFILE=/var/lib/openhands-agent/browser/chromium-profile",
        "XDG_CONFIG_HOME=/var/lib/openhands-agent/browser/xdg-config",
        "XDG_CACHE_HOME=/var/lib/openhands-agent/browser/xdg-cache",
        "XDG_DATA_HOME=/var/lib/openhands-agent/browser/xdg-data",
    )
    browser_state_ok = browser_environment_status == 0 and all(
        value in browser_environment for value in browser_state_values
    )
    model_api_key_names = (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "BROWSER_USE_API_KEY",
    )
    antigravity_enabled = bool(config.providers.get("antigravity") and config.providers["antigravity"].enabled)
    if antigravity_enabled:
        antigravity_command = _command_check("Antigravity CLI", "agy")
        antigravity_bridge = _command_check("Antigravity ACP bridge", "/opt/antigravity-acp/bin/python")
        antigravity_version = _exact_version(
            "Antigravity version", ["agy", "--version"], versions.get("ANTIGRAVITY_VERSION", "1.1.5")
        )
    else:
        antigravity_command = Check(
            "Antigravity CLI", True, "optional provider disabled; executable probe skipped", required=False
        )
        antigravity_bridge = Check(
            "Antigravity ACP bridge", True, "optional provider disabled; bridge probe skipped", required=False
        )
        antigravity_version = Check(
            "Antigravity version", True, "optional provider disabled; version probe skipped", required=False
        )
    worker_gh_exposed, worker_gh_detail = _credential_exposure(Path("/var/lib/openhands-agent/.config/gh/hosts.yml"))
    checks = [
        Check(
            "config allowlist",
            bool(config.github.allowed_repositories)
            and all("CHANGE_ME" not in repository for repository in config.github.allowed_repositories),
            ", ".join(config.github.allowed_repositories),
        ),
        Check(
            "localhost service bind",
            config.service.listen_host in {"127.0.0.1", "::1", "localhost"},
            config.service.listen_host,
        ),
        Check(
            "state directory",
            config.service.state_dir.is_dir() and os.access(config.service.state_dir, os.W_OK),
            str(config.service.state_dir),
        ),
        Check("webhook secret", config.service.webhook_secret_file.is_file(), str(config.service.webhook_secret_file)),
        Check(
            "Canvas API key",
            config.service.agent_server_api_key_file.is_file(),
            str(config.service.agent_server_api_key_file),
        ),
        _canvas_key_permissions(config),
        _service_accounts(),
        _validator_boundary(config),
        _empty_setup_behavior(config, coordinator),
        Check(
            "QEMU guest agent",
            guest_agent_status == 0 and guest_agent_output == "active",
            (
                "active"
                if guest_agent_status == 0 and guest_agent_output == "active"
                else "inactive or unavailable; enable QEMU Guest Agent in the Proxmox VM options and reboot the VM"
            ),
            required=False,
        ),
        Check(
            "worker Secret Service keyring",
            keyring_status == 0 and keyring_output == "active",
            f"service={keyring_output or 'unknown'}, private bus=/run/openhands-agent/bus",
        ),
        Check(
            "private browser service",
            browser_status == 0 and browser_output == "active",
            _service_failure_detail("openhands-browser.service", browser_output),
        ),
        Check(
            "private browser writable state",
            browser_state_ok,
            (
                "Chromium profile and Crashpad/XDG state use the writable private browser directory"
                if browser_state_ok
                else browser_environment or "unable to read the installed browser service environment"
            ),
        ),
        Check(
            "private-port firewall",
            firewall_status == 0
            and firewall_output == "active"
            and nft_status == 0
            and all(port in nft_output for port in ("8000", "8787", "9222")),
            "nftables drops non-loopback ingress to 8000/8787/9222"
            if nft_status == 0
            else nft_output or firewall_output or "unknown",
        ),
        _workspace_permissions(config),
        _command_check("git", "git"),
        _command_check("GitHub CLI", "gh"),
        _command_check("Browser Use", "/opt/browser-use/bin/browser-use"),
        _command_check("Browser Harness", "/opt/browser-use/bin/browser-harness"),
        antigravity_command,
        _command_check("Claude ACP wrapper", "/opt/openhands-symphony/scripts/claude_acp_wrapper.sh"),
        _command_check("Codex ACP wrapper", "/opt/openhands-symphony/scripts/codex_acp_wrapper.sh"),
        antigravity_bridge,
        _github_version(versions),
        _exact_version("Node.js version", ["node", "--version"], versions.get("NODE_VERSION", "22.23.1")),
        _exact_version("uv version", ["uv", "--version"], versions.get("UV_VERSION", "0.11.29")),
        _exact_version(
            "Agent Canvas version",
            ["/opt/openhands-canvas/node_modules/.bin/agent-canvas", "--version"],
            versions.get("AGENT_CANVAS_VERSION", "1.4.0"),
        ),
        _exact_version(
            "Claude Code version",
            ["/opt/provider-clis/node_modules/.bin/claude", "--version"],
            versions.get("CLAUDE_CODE_VERSION", "2.1.205"),
        ),
        _exact_version(
            "Codex version",
            ["/opt/provider-clis/node_modules/.bin/codex", "--version"],
            versions.get("CODEX_VERSION", "0.144.4"),
        ),
        _exact_version(
            "Claude ACP version",
            [
                "node",
                "-p",
                "require('/opt/openhands-acp/node_modules/@agentclientprotocol/claude-agent-acp/package.json').version",
            ],
            versions.get("CLAUDE_ACP_VERSION", "0.59.0"),
        ),
        _exact_version(
            "Codex ACP version",
            [
                "node",
                "-p",
                "require('/opt/openhands-acp/node_modules/@agentclientprotocol/codex-acp/package.json').version",
            ],
            versions.get("CODEX_ACP_VERSION", "1.1.4"),
        ),
        antigravity_version,
        _exact_version(
            "Browser Use version",
            [
                "/opt/browser-use/tools/browser-use/bin/python",
                "-c",
                "import importlib.metadata; print(importlib.metadata.version('browser-use'))",
            ],
            versions.get("BROWSER_USE_VERSION", "0.13.4"),
        ),
        _exact_version(
            "Browser Harness version",
            [
                "/opt/browser-use/tools/browser-use/bin/python",
                "-c",
                "import importlib.metadata; print(importlib.metadata.version('browser-harness'))",
            ],
            versions.get("BROWSER_HARNESS_VERSION", "0.1.5"),
        ),
        Check(
            "Browser Use private home",
            canvas_environment_status == 0
            and all(
                value in canvas_environment
                for value in (
                    "BROWSER_USE_HOME=/var/lib/openhands-agent/browser",
                    "BROWSER_HARNESS_HOME=/var/lib/openhands-agent/browser",
                    "BU_CDP_URL=http://127.0.0.1:9222",
                    "BH_TELEMETRY=0",
                )
            ),
            "Canvas browser home/CDP/telemetry policy is configured"
            if all(
                value in canvas_environment
                for value in (
                    "BROWSER_USE_HOME=/var/lib/openhands-agent/browser",
                    "BROWSER_HARNESS_HOME=/var/lib/openhands-agent/browser",
                    "BU_CDP_URL=http://127.0.0.1:9222",
                    "BH_TELEMETRY=0",
                )
            )
            else "Canvas browser home/CDP/telemetry policy is incomplete",
        ),
        Check(
            "OpenHands automation pin",
            canvas_environment_status == 0
            and f"OH_AUTOMATION_VERSION={versions.get('OPENHANDS_AUTOMATION_VERSION', '1.1.6')}" in canvas_environment,
            f"expected OH_AUTOMATION_VERSION={versions.get('OPENHANDS_AUTOMATION_VERSION', '1.1.6')}",
        ),
        _browser_cdp(versions.get("CHROMIUM_VERSION", "149.0.7827.55")),
        _agent_server(config, versions.get("AGENT_SERVER_VERSION", "1.35.0")),
        Check(
            "model API keys absent",
            not any(os.environ.get(name) for name in model_api_key_names)
            and canvas_environment_status == 0
            and not any(f"{name}=" in canvas_environment for name in model_api_key_names),
            "API-key variables are absent from the orchestrator and Canvas service environments",
        ),
        Check(
            "legacy default GitHub credential absent",
            not (config.service.state_dir / ".config" / "gh" / "hosts.yml").exists(),
            "GitHub auth must live only in /var/lib/openhands-symphony/github",
        ),
        Check(
            "worker GitHub credential isolation",
            not worker_gh_exposed and "GH_CONFIG_DIR=" not in canvas_environment,
            f"{worker_gh_detail}; the model worker must not inherit the orchestrator GitHub login",
        ),
    ]
    try:
        with store.connect() as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        checks.append(Check("SQLite integrity", result == "ok", str(result)))
    except sqlite3.Error as exc:
        checks.append(Check("SQLite integrity", False, str(exc)))

    status, output = _run(["gh", "auth", "status", "--hostname", "github.com"])
    checks.append(Check("GitHub authentication", status == 0, output or "gh not installed"))
    for name, provider in coordinator.providers.items():
        auth = provider.auth_status()
        checks.append(Check(f"{name} authentication", auth.authenticated, auth.detail))
        healthy, detail = provider.health()
        checks.append(Check(f"{name} autonomous execution", healthy, detail))
        capabilities = provider.capabilities
        checks.append(
            Check(
                f"{name} capability contract",
                capabilities.supports_headless
                and capabilities.supports_subscription_login
                and capabilities.autonomous_available,
                json.dumps(capabilities.__dict__, sort_keys=True),
            )
        )
    return checks
