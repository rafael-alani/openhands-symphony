from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from .config import DEFAULT_CONFIG, load_config
from .doctor import run_doctor
from .graduation import (
    GRADUATION_LOCK,
    GhGraduationBackend,
    GraduationError,
    Graduator,
    guard_hack_campaign,
    render_plan,
)
from .models import IdeaProject, IdeaRun, Job
from .runtime import build_coordinator, validate_operational_config
from .store import Store


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


def _job_status_line(job: Job, report_dir: Path) -> str:
    report = report_dir / job.id / "run.md"
    return (
        f"{job.repository}#{job.issue_number} state={job.state} provider={job.implementation_provider} "
        f"attempt={job.attempt} phase={job.phase} run={job.id} "
        f"conversation={job.conversation_id or '-'} review_conversation={job.review_conversation_id or '-'} "
        f"pr={job.pr_url or '-'} report={report if report.is_file() else '-'}"
    )


def _idea_status_line(project: IdeaProject, run: IdeaRun | None, report_dir: Path) -> str:
    report = report_dir / run.id / "run.md" if run else None
    return (
        f"{project.repository} latest={project.latest_observed_spec_hash or '-'} "
        f"completed={project.latest_completed_spec_hash or '-'} state={run.state if run else '-'} "
        f"run={run.id if run else '-'} publication={run.published_commit if run and run.published_commit else '-'} "
        f"preview={project.preview_state} last_good={project.last_good_preview_commit or '-'} "
        f"question={run.question if run and run.state.value == 'question' else '-'} "
        f"report={report if report and report.is_file() else '-'}"
    )


def _hack_status(campaign: dict, tasks: list[dict]) -> None:
    print(
        f"hack={campaign['repository']} state={campaign['state']} campaign={campaign['id']} "
        f"home={campaign['home_tier']} branch={campaign['branch']} expires={campaign['expires_at']} "
        f"pr={campaign.get('pr_url') or '-'} note={campaign.get('note') or campaign.get('error') or '-'}"
    )
    for task in tasks:
        print(
            f"  task={task['id']} kind={task['kind']} lane={task['lane']} state={task['state']} "
            f"conversation={task.get('conversation_id') or '-'} note={task.get('note') or task.get('error') or '-'}"
        )


def _job_needs_explicit_retry(job: Job | None) -> bool:
    if job is None:
        return False
    retryable_pr = job.state.value == "pr-open"
    polluted_queued_attempts = (
        job.state.value == "queued" and job.attempt > 0 and not job.conversation_id and not job.pr_number
    )
    return (
        job.state.value in {"needs-guidance", "blocked", "failed", "canceled"}
        or retryable_pr
        or polluted_queued_attempts
    )


def _systemctl(action: str) -> int:
    if action == "start":
        target = "openhands-symphony.target"
        target_active = _run_interactive(["systemctl", "is-active", "--quiet", target]) == 0
        if target_active:
            required_units = (
                "openhands-symphony-firewall.service",
                "openhands-agent-dbus.service",
                "openhands-agent-keyring.service",
                "openhands-browser.service",
                "openhands-canvas.service",
                "openhands-idea-preview.service",
                "openhands-symphony.service",
                "openhands-symphony-reconcile.timer",
            )
            if all(
                _run_interactive(["systemctl", "is-active", "--quiet", unit]) == 0 for unit in required_units
            ):
                print("openhands-symphony stack is already active; no start needed")
                return 0
            print("openhands-symphony target is active but a required unit is not; restarting the stack")
            return _run_interactive(["systemctl", "restart", target])
    return _run_interactive(["systemctl", action, "openhands-symphony.target"])


def _target_is_active() -> bool:
    process = subprocess.run(
        ["systemctl", "is-active", "openhands-symphony.target"],
        text=True,
        capture_output=True,
        check=False,
    )
    state = process.stdout.strip()
    if process.returncode == 0 and state == "active":
        return True
    if state in {"inactive", "failed"}:
        return False
    detail = process.stderr.strip() or state or f"exit {process.returncode}"
    raise GraduationError(f"unable to establish a quiescent Symphony service: {detail}")


def _stop_for_graduation() -> int:
    """Stop every unit capable of accepting, executing, or serving Ideas work."""

    return _run_interactive(
        [
            "systemctl",
            "stop",
            "openhands-symphony.target",
            "openhands-symphony.service",
            "openhands-symphony-reconcile.timer",
            "openhands-symphony-reconcile.service",
            "openhands-idea-preview.service",
        ]
    )


def _graduate(config_value: str | None, repository: str, approval_id: str | None) -> int:
    config_path = Path(config_value or os.environ.get("SYMPHONY_CONFIG") or DEFAULT_CONFIG).expanduser().resolve()
    config = load_config(config_path)
    guard_hack_campaign(config, repository)
    if config.vault.enabled:
        vault_store = Store(config.service.state_dir / "state.db")
        if any(project["repository"] == repository for project in vault_store.vault_projects()):
            raise GraduationError("this repository is note-managed; set symphony: github in its note for a reversible switch")
    backend = GhGraduationBackend(config.ideas.repositories, private_only=config.ideas.private_only)
    if not approval_id:
        plan = Graduator(config, config_path, backend).plan(repository)
        print(render_plan(plan, config_path=config_path))
        return 0

    config.service.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(config.service.state_dir / "state.db")
    lock_owner = f"graduate-cli-{os.getpid()}"
    if not store.acquire_operation_lock(GRADUATION_LOCK, lock_owner, seconds=7200):
        raise GraduationError("another graduation operation is active")

    restart_required = False
    restart_failed = False
    try:
        _target_is_active()
        restart_required = True
        if _stop_for_graduation() != 0:
            raise GraduationError("unable to stop Symphony before graduation")
        result = Graduator(config, config_path, backend, store=store).apply(
            repository,
            approval_id,
            operation_lock_held=True,
        )
    finally:
        if restart_required and _systemctl("start") != 0:
            restart_failed = True
            print("graduation completed or rolled back, but Symphony could not be restarted", file=sys.stderr)
        store.release_operation_lock(GRADUATION_LOCK, lock_owner)

    print(f"graduated {repository} archive_commit={result.archive_commit}")
    print(f"retired_ideas_runs={len(result.retired_runs)}")
    for url in result.issue_urls:
        print(f"draft_issue={url}")
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 1 if restart_failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="agentctl")
    parser.add_argument("--config", default=os.environ.get("SYMPHONY_CONFIG"), help="path to config.toml")
    subparsers = parser.add_subparsers(dest="command", required=True)
    auth = subparsers.add_parser("auth")
    auth.add_argument("provider", choices=["claude", "codex", "antigravity", "github"])
    settings = subparsers.add_parser("settings", help="show effective agent defaults without starting a run")
    settings.add_argument("--provider", default="codex")
    settings.add_argument("--repository", default="")
    for name in ("doctor", "start", "stop", "restart", "status", "logs", "update", "reconcile", "labels"):
        subparsers.add_parser(name)
    run = subparsers.add_parser("run")
    run.add_argument("item", type=_parse_item)
    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("item", type=_parse_item)
    graduate = subparsers.add_parser("graduate")
    graduate.add_argument("repository")
    graduate.add_argument("--approve", metavar="PLAN_ID", help="apply the exact previously reviewed dry-run plan")
    hack = subparsers.add_parser("hack", help="operate a bounded parallel campaign")
    hack_commands = hack.add_subparsers(dest="hack_command", required=True)
    hack_start = hack_commands.add_parser("start", help="suspend normal intake and queue the solo scaffold")
    hack_start.add_argument("repository")
    hack_start.add_argument("--hours", type=float, default=24, help="hard campaign deadline, up to 168 hours")
    hack_stop = hack_commands.add_parser("stop", help="stop fan-out, drain, polish and publish once")
    hack_stop.add_argument("repository")
    hack_status = hack_commands.add_parser("status", help="show durable campaign and task state")
    hack_status.add_argument("repository", nargs="?")
    vault_check = subparsers.add_parser("vault-check", help="validate a project note and linked checklist without changing anything")
    vault_check.add_argument("note", type=Path)
    vault_check.add_argument("--vault-root", type=Path, help="vault root for Obsidian vault-relative links")
    args = parser.parse_args()

    if args.command == "vault-check":
        from .ideas_contract import git_blob_hash, validate_spec
        from .vault import read_note
        from .vault_project import VaultError, compile_project

        try:
            note = read_note(args.note.absolute())
            if note is None:
                raise VaultError("main note needs symphony: idea, github, or paused in YAML")
            repository = note.repository or "preview/project"
            snapshot = compile_project(note, repository, (args.vault_root or note.path.parent).absolute())
            snapshot.verify()
            validate_spec(snapshot.spec, repository)
            print("Project input is valid (read-only; no repository creation or agent execution).")
            print(f"mode: {note.mode}; repository: {note.repository or 'created automatically on intake'}")
            print(f"agent overrides: {json.dumps(note.settings.values())} (omitted values inherit defaults)")
            print(f"spec: {git_blob_hash(snapshot.spec)}; checklist files: {len(snapshot.files)}")
            for item in snapshot.files:
                print(f"[{'x' if item.checked else ' '}] {item.key}")
        except (ValueError, OSError) as exc:
            print(f"project input error: {exc}", file=sys.stderr)
            raise SystemExit(2) from None
        raise SystemExit(0)

    if args.command == "auth":
        raise SystemExit(_authenticate_provider(args.provider))
    if args.command == "settings":
        try:
            config = load_config(args.config)
            effective = config.agent_settings(args.provider, args.repository)
            print(json.dumps({"provider": args.provider, "repository": args.repository or None,
                              **effective.values()}, indent=2))
        except (ValueError, KeyError, OSError) as exc:
            print(f"settings error: {exc}", file=sys.stderr)
            raise SystemExit(2) from None
        raise SystemExit(0)
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
    if args.command == "graduate":
        try:
            raise SystemExit(_graduate(args.config, args.repository, args.approve))
        except Exception as exc:
            print(f"graduation error: {exc}", file=sys.stderr)
            raise SystemExit(2) from None

    try:
        config, store, coordinator = build_coordinator(args.config)
    except Exception as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    if args.command == "doctor":
        coordinator.ideas.preview_deployments.sync_store(store, config.ideas.repositories)
        checks = run_doctor(config, store, coordinator)
        for check in checks:
            marker = "PASS" if check.ok else ("WARN" if not check.required else "FAIL")
            print(f"[{marker}] {check.name}: {check.detail}")
        raise SystemExit(0 if all(check.ok or not check.required for check in checks) else 1)
    if args.command == "hack":
        try:
            if args.hack_command == "start":
                coordinator.refresh_vault()
                campaign = coordinator.hack.start(args.repository, hours=args.hours)
                _hack_status(campaign, coordinator.hack.state.list_tasks(campaign["id"]))
            elif args.hack_command == "stop":
                campaign = coordinator.hack.stop(args.repository)
                if campaign:
                    _hack_status(campaign, coordinator.hack.state.list_tasks(campaign["id"]))
                else:
                    print(f"no active campaign: {args.repository}")
            else:
                for campaign in coordinator.hack.state.list_campaigns(active_only=False):
                    if not args.repository or campaign["repository"] == args.repository:
                        _hack_status(campaign, coordinator.hack.state.list_tasks(campaign["id"]))
        except Exception as exc:
            from .validation import redact

            print(f"hack error: {redact(str(exc), 2000)}", file=sys.stderr)
            raise SystemExit(2) from None
        raise SystemExit(0)
    if args.command == "status":
        coordinator.ideas.preview_deployments.sync_store(store, config.ideas.repositories)
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
            print(_job_status_line(job, config.service.report_dir))
        projects = store.list_idea_projects()
        runs = store.list_idea_runs()
        latest = {project.repository: None for project in projects}
        for run in runs:
            latest[run.repository] = run
        print(f"ideas={len(projects)}")
        for project in projects:
            print(_idea_status_line(project, latest[project.repository], config.service.report_dir))
        for project in store.vault_projects():
            print(f"vault={project['repository']} mode={project['mode']} desired={project['desired_mode']} status={project['status']} note={project['note_path']}")
        if coordinator.hack:
            for campaign in coordinator.hack.state.list_campaigns():
                _hack_status(campaign, coordinator.hack.state.list_tasks(campaign["id"]))
        raise SystemExit(0)
    if args.command == "reconcile":
        if coordinator.hack:
            coordinator.hack.reconcile()
        coordinator.refresh_vault()
        for repository, issue_number, result in coordinator.reconcile():
            item = f"{repository}#{issue_number}" if issue_number else repository
            print(f"{item}: {result}")
        for repository, result in coordinator.ideas.reconcile():
            print(f"{repository}: {result}")
        raise SystemExit(0)
    if args.command == "labels":
        from .labels import LABEL_CONTRACT

        for repository in config.github.allowed_repositories:
            changed = coordinator.github.ensure_contract_labels(repository, LABEL_CONTRACT)
            if changed:
                print(f"{repository}: labels ready ({changed} created or updated)")
            else:
                print(f"{repository}: labels already ready; no changes needed")
        raise SystemExit(0)
    if args.command == "run":
        repository, issue_number = args.item
        snapshot = coordinator.github.get_issue(repository, issue_number)
        existing = store.get_job(repository, issue_number)
        if _job_needs_explicit_retry(existing):
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
