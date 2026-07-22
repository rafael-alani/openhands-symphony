from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from .config import load_config
from .doctor import run_doctor
from .runtime import build_coordinator, validate_operational_config


def _run_interactive(command: list[str], *, environment: dict[str, str] | None = None) -> int:
    return subprocess.run(command, check=False, env=environment).returncode


def _authentication_environment(provider: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    if provider != "github":
        environment.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/openhands-agent/bus")
    if provider == "github":
        environment.setdefault("GH_CONFIG_DIR", "/var/lib/openhands-symphony/github")
    if provider == "antigravity":
        # sudo -iu normally removes the caller's SSH_* variables. Antigravity
        # uses remote-session detection to select its manual URL/code OAuth loop.
        environment.setdefault("SSH_CONNECTION", "127.0.0.1 0 127.0.0.1 0")
        environment.setdefault("SSH_CLIENT", "127.0.0.1 0 0")
        environment.setdefault("SSH_TTY", "/dev/tty")
    return environment


def _antigravity_cpu_error(*, machine: str | None = None, cpuinfo: str | None = None) -> str | None:
    machine = (machine or platform.machine()).lower()
    if machine not in {"amd64", "x86_64"}:
        return None
    if cpuinfo is None:
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text()
        except OSError:
            return None
    if "pclmulqdq" in cpuinfo.lower().split():
        return None
    return (
        "Antigravity CLI cannot run because this x86_64 VM does not expose PCLMULQDQ. "
        "Keep providers.antigravity.enabled=false or update the VM CPU model before authenticating it."
    )


def _authenticate_provider(provider: str) -> int:
    login_commands = {
        "claude": ["/opt/provider-clis/node_modules/.bin/claude", "auth", "login"],
        "codex": ["/opt/provider-clis/node_modules/.bin/codex", "login", "--device-auth"],
        "antigravity": ["agy"],
        "github": ["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"],
    }
    verify_commands = {
        "claude": ["/opt/provider-clis/node_modules/.bin/claude", "auth", "status"],
        "codex": ["/opt/provider-clis/node_modules/.bin/codex", "login", "status"],
        "antigravity": ["agy", "models"],
        "github": ["gh", "auth", "status", "--hostname", "github.com"],
    }
    environment = _authentication_environment(provider)
    if provider == "antigravity" and (cpu_error := _antigravity_cpu_error()):
        print(cpu_error, file=sys.stderr)
        return 2
    status = _run_interactive(verify_commands[provider], environment=environment)
    if status == 0:
        if provider == "github":
            status = _run_interactive(["gh", "auth", "setup-git"], environment=environment)
            if status:
                return status
        else:
            _write_auth_marker(provider)
        print(f"{provider} is already authenticated; no login needed")
        return 0
    if provider == "antigravity":
        print(
            "Antigravity SSH login: open the printed authorization URL in your local browser, then paste only "
            "the alphanumeric authorization code shown by the browser into this terminal. Do not paste a URL."
        )
    status = _run_interactive(login_commands[provider], environment=environment)
    if status:
        return status
    status = _run_interactive(verify_commands[provider], environment=environment)
    if status:
        return status
    if provider == "github":
        return _run_interactive(["gh", "auth", "setup-git"], environment=environment)
    _write_auth_marker(provider)
    print(f"{provider} subscription authentication verified with the official CLI")
    return 0


def _write_auth_marker(provider: str) -> None:
    marker_dir = Path(os.environ.get("SYMPHONY_AUTH_MARKER_DIR", "/var/lib/openhands-auth-status"))
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f"{provider}.json"
    marker.write_text(
        json.dumps(
            {
                "provider": provider,
                "verified_at": datetime.now(UTC).isoformat(),
                "verification": "official CLI status command exited 0",
            },
            sort_keys=True,
        )
        + "\n"
    )
    marker.chmod(0o640)


def _parse_item(value: str) -> tuple[str, int]:
    if "#" not in value:
        raise argparse.ArgumentTypeError("item must use owner/repository#issue format")
    repository, number = value.rsplit("#", 1)
    if not number.isdigit() or int(number) < 1:
        raise argparse.ArgumentTypeError("issue number must be positive")
    return repository, int(number)


def _systemctl(action: str) -> int:
    return _run_interactive(["systemctl", action, "openhands-symphony.target"])


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentctl")
    parser.add_argument("--config", default=os.environ.get("SYMPHONY_CONFIG"), help="path to config.toml")
    subparsers = parser.add_subparsers(dest="command", required=True)
    auth = subparsers.add_parser("auth")
    auth.add_argument("provider", choices=["claude", "codex", "antigravity", "github"])
    for name in ("doctor", "start", "stop", "restart", "status", "logs", "update", "reconcile", "labels"):
        subparsers.add_parser(name)
    run = subparsers.add_parser("run")
    run.add_argument("item", type=_parse_item)
    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("item", type=_parse_item)
    args = parser.parse_args()

    if args.command == "auth":
        raise SystemExit(_authenticate_provider(args.provider))
    if args.command == "stop":
        raise SystemExit(_systemctl(args.command))
    if args.command in {"start", "restart"}:
        try:
            validate_operational_config(load_config(args.config))
        except Exception as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            raise SystemExit(2) from None
        raise SystemExit(_systemctl(args.command))
    if args.command == "logs":
        raise SystemExit(_run_interactive(["journalctl", "-u", "openhands-symphony.service", "-f", "-n", "100"]))
    if args.command == "update":
        source_path_file = Path("/etc/openhands-symphony/source-path")
        source_path = Path(source_path_file.read_text().strip()) if source_path_file.is_file() else None
        installer = (source_path / "install.sh") if source_path else Path("/opt/openhands-symphony/install.sh")
        if not installer.is_file():
            print(
                f"update installer is missing: {installer}; rerun sudo ./install.sh from the source checkout",
                file=sys.stderr,
            )
            raise SystemExit(1)
        command = [str(installer), "--update"] if os.geteuid() == 0 else ["sudo", str(installer), "--update"]
        raise SystemExit(_run_interactive(command))

    try:
        config, store, coordinator = build_coordinator(args.config)
    except Exception as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    if args.command == "doctor":
        checks = run_doctor(config, store, coordinator)
        for check in checks:
            marker = "PASS" if check.ok else ("WARN" if not check.required else "FAIL")
            print(f"[{marker}] {check.name}: {check.detail}")
        raise SystemExit(0 if all(check.ok or not check.required for check in checks) else 1)
    if args.command == "status":
        active = subprocess.run(
            ["systemctl", "is-active", "openhands-symphony.target"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        print(f"service={active.stdout.strip() or 'unknown'}")
        jobs = store.list_jobs()
        print(f"jobs={len(jobs)}")
        for job in jobs:
            print(
                f"{job.repository}#{job.issue_number} state={job.state} provider={job.implementation_provider} "
                f"attempt={job.attempt} run={job.id} pr={job.pr_url or '-'}"
            )
        raise SystemExit(0)
    if args.command == "reconcile":
        for repository, issue_number, result in coordinator.reconcile():
            item = f"{repository}#{issue_number}" if issue_number else repository
            print(f"{item}: {result}")
        raise SystemExit(0)
    if args.command == "labels":
        from .labels import LABEL_CONTRACT

        for repository in config.github.allowed_repositories:
            coordinator.github.ensure_contract_labels(repository, LABEL_CONTRACT)
            print(f"{repository}: labels ready")
        raise SystemExit(0)
    if args.command == "run":
        repository, issue_number = args.item
        snapshot = coordinator.github.get_issue(repository, issue_number)
        existing = store.get_job(repository, issue_number)
        retryable_review = (
            existing
            and existing.state.value == "pr-open"
            and existing.review_required
            and existing.phase != "review-complete"
        )
        if existing and (
            existing.state.value in {"needs-guidance", "blocked", "failed", "canceled"} or retryable_review
        ):
            job = coordinator.control(repository, issue_number, "retry")
            print(f"requeued run={job.id if job else '-'}")
        else:
            job, created = coordinator.enqueue(snapshot)
            print(f"{'created' if created else 'coalesced'} run={job.id} state={job.state}")
        raise SystemExit(0)
    if args.command == "cancel":
        repository, issue_number = args.item
        job = coordinator.control(repository, issue_number, "cancel")
        print(f"canceled run={job.id if job else '-'}")
        raise SystemExit(0)


if __name__ == "__main__":
    main()
