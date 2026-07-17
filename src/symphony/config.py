from __future__ import annotations

import os
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = "/etc/openhands-symphony/config.toml"
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class ServiceConfig:
    state_dir: Path = Path("/var/lib/openhands-symphony")
    workspace_dir: Path = Path("/var/lib/openhands-symphony/workspaces")
    report_dir: Path = Path("/var/lib/openhands-symphony/reports")
    log_dir: Path = Path("/var/log/openhands-symphony")
    listen_host: str = "127.0.0.1"
    listen_port: int = 8787
    webhook_secret_file: Path = Path("/etc/openhands-symphony/webhook-secret")
    agent_server_url: str = "http://127.0.0.1:8000"
    agent_server_api_key_file: Path = Path("/etc/openhands-symphony/canvas.env")
    global_agent_instruction: str = ""
    validation_user: str = "openhands-validator"


@dataclass(frozen=True)
class GitHubConfig:
    allowed_repositories: tuple[str, ...] = ()
    private_only: bool = True
    auth_mode: str = "gh"
    generated_pr_label: str = "generated-by-agent"
    bot_login: str = ""


@dataclass(frozen=True)
class SchedulerConfig:
    poll_seconds: int = 60
    reconcile_seconds: int = 300
    lease_seconds: int = 180
    heartbeat_seconds: int = 30
    global_concurrency: int = 2
    max_attempts: int = 3
    max_implementation_corrections: int = 1
    max_review_repairs: int = 1
    validation_timeout_seconds: int = 1800
    provider_backoff_base_seconds: int = 120
    provider_backoff_max_seconds: int = 3600
    provider_concurrency: dict[str, int] = field(default_factory=lambda: {"claude": 1, "codex": 1, "antigravity": 1})


@dataclass(frozen=True)
class ProviderConfig:
    enabled: bool
    adapter: str
    acp_command: tuple[str, ...]
    auth_command: tuple[str, ...]
    auth_marker_file: Path | None = None
    timeout_seconds: int = 7200
    manual_command: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepositoryConfig:
    concurrency_scope: str = "repository"
    concurrency_key: str = ""
    concurrency_labels: dict[str, str] = field(default_factory=dict)
    validation_commands: tuple[tuple[str, ...], ...] = ()
    setup_script: str = ".openhands/setup.sh"
    instruction: str = ""
    approval_policy: str = "safe-code-only"


@dataclass(frozen=True)
class Config:
    service: ServiceConfig
    github: GitHubConfig
    scheduler: SchedulerConfig
    providers: dict[str, ProviderConfig]
    repositories: dict[str, RepositoryConfig]

    def repository(self, name: str) -> RepositoryConfig:
        return self.repositories.get(name, RepositoryConfig())

    def concurrency_key(self, repository: str, labels: tuple[str, ...] = ()) -> str:
        cfg = self.repository(repository)
        if cfg.concurrency_scope == "configured":
            return cfg.concurrency_key
        if cfg.concurrency_scope == "label":
            selected = [key for label, key in cfg.concurrency_labels.items() if label in labels]
            if len(selected) != 1:
                raise ValueError("exactly one configured concurrency-scope label is required")
            return f"{repository}:{selected[0]}"
        return repository


def _path(value: Any, default: Path) -> Path:
    return Path(os.path.expanduser(str(value))) if value is not None else default


def _command(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(shlex.split(value))
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValueError("commands must be a shell-like string or an array of strings")


def _validation_commands(value: Any) -> tuple[tuple[str, ...], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("validation_commands must be an array")
    return tuple(_command(command) for command in value)


def _validate_config(config: Config) -> None:
    if config.service.listen_host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("service.listen_host must be loopback; use an SSH tunnel or Tailscale for access")
    if not 1 <= config.service.listen_port <= 65535:
        raise ValueError("service.listen_port must be between 1 and 65535")
    if config.github.auth_mode != "gh":
        raise ValueError("github.auth_mode currently supports only 'gh'")
    if config.service.validation_user and not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", config.service.validation_user):
        raise ValueError("service.validation_user must be an empty string or a safe local account name")
    if not config.github.allowed_repositories:
        raise ValueError("github.allowed_repositories must contain at least one repository")
    if len(set(config.github.allowed_repositories)) != len(config.github.allowed_repositories):
        raise ValueError("github.allowed_repositories contains duplicates")
    for repository in config.github.allowed_repositories:
        if not REPOSITORY_PATTERN.fullmatch(repository) or ".." in repository:
            raise ValueError(f"invalid GitHub repository identifier: {repository!r}")

    scheduler = config.scheduler
    positive = {
        "poll_seconds": scheduler.poll_seconds,
        "reconcile_seconds": scheduler.reconcile_seconds,
        "lease_seconds": scheduler.lease_seconds,
        "heartbeat_seconds": scheduler.heartbeat_seconds,
        "global_concurrency": scheduler.global_concurrency,
        "max_attempts": scheduler.max_attempts,
        "validation_timeout_seconds": scheduler.validation_timeout_seconds,
        "provider_backoff_base_seconds": scheduler.provider_backoff_base_seconds,
        "provider_backoff_max_seconds": scheduler.provider_backoff_max_seconds,
    }
    for name, value in positive.items():
        if value <= 0:
            raise ValueError(f"scheduler.{name} must be greater than zero")
    if scheduler.lease_seconds <= scheduler.heartbeat_seconds:
        raise ValueError("scheduler.lease_seconds must exceed scheduler.heartbeat_seconds")
    if scheduler.provider_backoff_max_seconds < scheduler.provider_backoff_base_seconds:
        raise ValueError("scheduler.provider_backoff_max_seconds must be at least the base backoff")
    if scheduler.max_implementation_corrections < 0 or scheduler.max_review_repairs < 0:
        raise ValueError("bounded correction and repair limits cannot be negative")
    for name, value in scheduler.provider_concurrency.items():
        if value < 0:
            raise ValueError(f"scheduler.provider_concurrency.{name} cannot be negative")

    for name, provider in config.providers.items():
        if provider.adapter != "openhands-acp":
            raise ValueError(f"unsupported provider adapter for {name}: {provider.adapter}")
        if provider.timeout_seconds <= 0:
            raise ValueError(f"providers.{name}.timeout_seconds must be greater than zero")
        if provider.enabled and (not provider.acp_command or not provider.auth_command):
            raise ValueError(f"enabled provider {name!r} requires acp_command and auth_command")
    unknown_limits = set(scheduler.provider_concurrency) - set(config.providers)
    if unknown_limits:
        raise ValueError(f"provider concurrency configured for unknown providers: {sorted(unknown_limits)}")

    unknown = set(config.repositories) - set(config.github.allowed_repositories)
    if unknown:
        raise ValueError(f"repository configuration is not allowlisted: {sorted(unknown)}")
    for name, repository in config.repositories.items():
        if repository.concurrency_scope not in {"repository", "configured", "label"}:
            raise ValueError(f"repositories.{name}.concurrency_scope must be 'repository', 'configured', or 'label'")
        if repository.concurrency_scope == "configured" and not repository.concurrency_key:
            raise ValueError(f"repositories.{name}.concurrency_key is required for configured scope")
        if repository.concurrency_scope == "label" and not repository.concurrency_labels:
            raise ValueError(f"repositories.{name}.concurrency_labels is required for label scope")
        for label, key in repository.concurrency_labels.items():
            if not label or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", label):
                raise ValueError(f"repositories.{name}.concurrency_labels contains an invalid label: {label!r}")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key):
                raise ValueError(f"repositories.{name}.concurrency_labels contains an invalid key: {key!r}")
        setup = Path(repository.setup_script)
        if setup.is_absolute() or ".." in setup.parts:
            raise ValueError(f"repositories.{name}.setup_script must be a confined relative path")
        if repository.approval_policy != "safe-code-only":
            raise ValueError(
                f"repositories.{name}.approval_policy only supports 'safe-code-only'; destructive work is never autonomous"
            )


def load_config(path: str | Path | None = None) -> Config:
    target = Path(path or os.environ.get("SYMPHONY_CONFIG", DEFAULT_CONFIG))
    with target.open("rb") as handle:
        raw = tomllib.load(handle)

    service_raw = raw.get("service", {})
    service = ServiceConfig(
        state_dir=_path(service_raw.get("state_dir"), ServiceConfig.state_dir),
        workspace_dir=_path(service_raw.get("workspace_dir"), ServiceConfig.workspace_dir),
        report_dir=_path(service_raw.get("report_dir"), ServiceConfig.report_dir),
        log_dir=_path(service_raw.get("log_dir"), ServiceConfig.log_dir),
        listen_host=str(service_raw.get("listen_host", "127.0.0.1")),
        listen_port=int(service_raw.get("listen_port", 8787)),
        webhook_secret_file=_path(service_raw.get("webhook_secret_file"), ServiceConfig.webhook_secret_file),
        agent_server_url=str(service_raw.get("agent_server_url", "http://127.0.0.1:8000")).rstrip("/"),
        agent_server_api_key_file=_path(
            service_raw.get("agent_server_api_key_file"), ServiceConfig.agent_server_api_key_file
        ),
        global_agent_instruction=str(service_raw.get("global_agent_instruction", "")),
        validation_user=str(service_raw.get("validation_user", "openhands-validator")),
    )

    github_raw = raw.get("github", {})
    github = GitHubConfig(
        allowed_repositories=tuple(github_raw.get("allowed_repositories", [])),
        private_only=bool(github_raw.get("private_only", True)),
        auth_mode=str(github_raw.get("auth_mode", "gh")),
        generated_pr_label=str(github_raw.get("generated_pr_label", "generated-by-agent")),
        bot_login=str(github_raw.get("bot_login", "")),
    )

    scheduler_raw = raw.get("scheduler", {})
    scheduler = SchedulerConfig(
        poll_seconds=int(scheduler_raw.get("poll_seconds", 60)),
        reconcile_seconds=int(scheduler_raw.get("reconcile_seconds", 300)),
        lease_seconds=int(scheduler_raw.get("lease_seconds", 180)),
        heartbeat_seconds=int(scheduler_raw.get("heartbeat_seconds", 30)),
        global_concurrency=int(scheduler_raw.get("global_concurrency", 2)),
        max_attempts=int(scheduler_raw.get("max_attempts", 3)),
        max_implementation_corrections=int(scheduler_raw.get("max_implementation_corrections", 1)),
        max_review_repairs=int(scheduler_raw.get("max_review_repairs", 1)),
        validation_timeout_seconds=int(scheduler_raw.get("validation_timeout_seconds", 1800)),
        provider_backoff_base_seconds=int(scheduler_raw.get("provider_backoff_base_seconds", 120)),
        provider_backoff_max_seconds=int(scheduler_raw.get("provider_backoff_max_seconds", 3600)),
        provider_concurrency={
            str(k): int(v)
            for k, v in scheduler_raw.get("provider_concurrency", {"claude": 1, "codex": 1, "antigravity": 1}).items()
        },
    )

    providers: dict[str, ProviderConfig] = {}
    for name, value in raw.get("providers", {}).items():
        providers[name] = ProviderConfig(
            enabled=bool(value.get("enabled", False)),
            adapter=str(value.get("adapter", "openhands-acp")),
            acp_command=_command(value.get("acp_command")),
            auth_command=_command(value.get("auth_command")),
            auth_marker_file=(
                _path(value.get("auth_marker_file"), Path("/nonexistent")) if value.get("auth_marker_file") else None
            ),
            timeout_seconds=int(value.get("timeout_seconds", 7200)),
            manual_command=_command(value.get("manual_command")),
        )

    repositories: dict[str, RepositoryConfig] = {}
    for name, value in raw.get("repositories", {}).items():
        repositories[name] = RepositoryConfig(
            concurrency_scope=str(value.get("concurrency_scope", "repository")),
            concurrency_key=str(value.get("concurrency_key", "")),
            concurrency_labels={str(label): str(key) for label, key in value.get("concurrency_labels", {}).items()},
            validation_commands=_validation_commands(value.get("validation_commands")),
            setup_script=str(value.get("setup_script", ".openhands/setup.sh")),
            instruction=str(value.get("instruction", "")),
            approval_policy=str(value.get("approval_policy", "safe-code-only")),
        )

    config = Config(service, github, scheduler, providers, repositories)
    _validate_config(config)
    return config
