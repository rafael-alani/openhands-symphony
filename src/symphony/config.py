from __future__ import annotations

import os
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_settings import INHERIT_SETTINGS, AgentSettings

DEFAULT_CONFIG = "/etc/openhands-symphony/config.toml"
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class ServiceConfig:
    state_dir: Path = Path("/var/lib/openhands-symphony")
    workspace_dir: Path = Path("/var/lib/openhands-symphony/workspaces")
    report_dir: Path = Path("/var/lib/openhands-symphony/reports")
    preview_dir: Path = Path("/var/lib/openhands-preview")
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
class IdeasConfig:
    repositories: tuple[str, ...] = ()
    private_only: bool = True
    spec_path: str = "idea/SPEC.md"
    progress_path: str = "idea/PROGRESS.md"


@dataclass(frozen=True)
class VaultConfig:
    enabled: bool = False
    path: Path = Path("/obsidian")
    projects_dir: str = "1. Projects & Tasks"
    owner: str = ""
    provider: str = "codex"
    quiet_seconds: int = 30
    port_start: int = 10000
    port_end: int = 10999
    manage_checkboxes: bool = True


@dataclass(frozen=True)
class HackConfig:
    enabled: bool = False
    repositories: tuple[str, ...] = ()
    provider: str = "codex"
    max_parallel: int = 4
    max_tasks: int = 100
    reserve_slots: int = 1
    task_timeout_seconds: int = 1800
    fast_gate_timeout_seconds: int = 300
    polish_seconds: int = 300
    milestone_every: int = 5
    publish_ideas: bool = False


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
    permission_mode: str = "full"
    reasoning_effort: str | None = None
    speed: str | None = None


@dataclass(frozen=True)
class RepositoryConfig:
    concurrency_scope: str = "repository"
    concurrency_key: str = ""
    concurrency_labels: dict[str, str] = field(default_factory=dict)
    validation_commands: tuple[tuple[str, ...], ...] = ()
    setup_script: str = ""
    instruction: str = ""
    approval_policy: str = "safe-code-only"
    reasoning_effort: str | None = None
    speed: str | None = None


@dataclass(frozen=True)
class Config:
    service: ServiceConfig
    github: GitHubConfig
    scheduler: SchedulerConfig
    providers: dict[str, ProviderConfig]
    repositories: dict[str, RepositoryConfig]
    ideas: IdeasConfig = IdeasConfig()
    vault: VaultConfig = VaultConfig()
    hack: HackConfig = HackConfig()

    def repository(self, name: str) -> RepositoryConfig:
        return self.repositories.get(name, RepositoryConfig())

    def agent_settings(self, provider: str, repository: str = "", override: AgentSettings = INHERIT_SETTINGS) -> AgentSettings:
        defaults = AgentSettings("xhigh", "normal") if provider == "codex" else AgentSettings()
        configured = self.providers[provider]
        defaults = defaults.overlay(AgentSettings(configured.reasoning_effort, configured.speed))
        repo = self.repository(repository)
        return defaults.overlay(AgentSettings(repo.reasoning_effort, repo.speed)).overlay(override).for_provider(provider)

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


def _repository_path(value: str, name: str) -> str:
    path = Path(value)
    if (
        not value
        or path == Path(".")
        or path.is_absolute()
        or ".." in path.parts
        or path.parts[0] == ".git"
        or path.as_posix() != value
    ):
        raise ValueError(f"ideas.{name} must be a confined repository-relative POSIX path")
    return value


def _paths_overlap(first: Path, second: Path) -> bool:
    left = first.resolve(strict=False)
    right = second.resolve(strict=False)
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _validate_config(config: Config) -> None:
    if config.service.listen_host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("service.listen_host must be loopback; use an SSH tunnel or Tailscale for access")
    if not 1 <= config.service.listen_port <= 65535:
        raise ValueError("service.listen_port must be between 1 and 65535")
    if config.github.auth_mode != "gh":
        raise ValueError("github.auth_mode currently supports only 'gh'")
    if config.service.validation_user and not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", config.service.validation_user):
        raise ValueError("service.validation_user must be an empty string or a safe local account name")
    if not config.service.preview_dir.is_absolute() or config.service.preview_dir == Path("/"):
        raise ValueError("service.preview_dir must be a dedicated absolute directory")
    protected_paths = (
        config.service.state_dir,
        config.service.workspace_dir,
        config.service.report_dir,
        config.service.log_dir,
    )
    if any(_paths_overlap(config.service.preview_dir, path) for path in protected_paths):
        raise ValueError("service.preview_dir must not overlap orchestrator state, workspaces, reports, or logs")
    if not config.github.allowed_repositories and not config.ideas.repositories and not config.vault.enabled:
        raise ValueError("github.allowed_repositories must contain at least one repository")
    if len(set(config.github.allowed_repositories)) != len(config.github.allowed_repositories):
        raise ValueError("github.allowed_repositories contains duplicates")
    for repository in config.github.allowed_repositories:
        if not REPOSITORY_PATTERN.fullmatch(repository) or ".." in repository:
            raise ValueError(f"invalid GitHub repository identifier: {repository!r}")
    if not config.ideas.private_only:
        raise ValueError("ideas.private_only must be true; direct pushes to public repositories are forbidden")
    if len(set(config.ideas.repositories)) != len(config.ideas.repositories):
        raise ValueError("ideas.repositories contains duplicates")
    for repository in config.ideas.repositories:
        if not REPOSITORY_PATTERN.fullmatch(repository) or ".." in repository:
            raise ValueError(f"invalid ideas repository identifier: {repository!r}")
    overlap = set(config.github.allowed_repositories) & set(config.ideas.repositories)
    if overlap:
        raise ValueError(f"repositories cannot be in both Tier 1 and ideas allowlists: {sorted(overlap)}")
    _repository_path(config.ideas.spec_path, "spec_path")
    _repository_path(config.ideas.progress_path, "progress_path")
    if config.ideas.spec_path == config.ideas.progress_path:
        raise ValueError("ideas.spec_path and ideas.progress_path must be different")
    if config.vault.enabled:
        if not config.vault.path.is_absolute() or config.vault.path == Path("/"):
            raise ValueError("vault.path must be a dedicated absolute directory")
        _repository_path(config.vault.projects_dir, "vault.projects_dir")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", config.vault.owner):
            raise ValueError("vault.owner must be a GitHub account or organization")
        if config.vault.provider not in config.providers or not config.providers[config.vault.provider].enabled:
            raise ValueError("vault.provider must name an enabled provider")
        if config.vault.quiet_seconds < 0 or not 1024 <= config.vault.port_start <= config.vault.port_end <= 65535:
            raise ValueError("invalid vault quiet period or preview port range")

    hack = config.hack
    if len(set(hack.repositories)) != len(hack.repositories):
        raise ValueError("hack.repositories contains duplicates")
    for repository in hack.repositories:
        if not REPOSITORY_PATTERN.fullmatch(repository) or ".." in repository:
            raise ValueError(f"invalid hack repository identifier: {repository!r}")
    if not 1 <= hack.max_parallel <= 6:
        raise ValueError("hack.max_parallel must be between 1 and 6")
    for name in ("max_tasks", "task_timeout_seconds", "fast_gate_timeout_seconds", "polish_seconds", "milestone_every"):
        if getattr(hack, name) <= 0:
            raise ValueError(f"hack.{name} must be greater than zero")
    if hack.reserve_slots < 1:
        raise ValueError("hack.reserve_slots must preserve at least one slot for GitHub work")
    if hack.enabled:
        if not hack.repositories:
            raise ValueError("hack.repositories must explicitly allowlist campaign repositories")
        if hack.provider not in config.providers or not config.providers[hack.provider].enabled:
            raise ValueError("hack.provider must name an enabled provider")
        if config.scheduler.global_concurrency <= hack.reserve_slots:
            raise ValueError("scheduler.global_concurrency must exceed hack.reserve_slots")
        if config.scheduler.provider_concurrency.get(hack.provider, 1) <= 0:
            raise ValueError("hack.provider must have positive provider concurrency")

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
        if provider.permission_mode not in {"full", "restricted"}:
            raise ValueError(f"providers.{name}.permission_mode must be full or restricted")
        if provider.adapter != "openhands-acp":
            raise ValueError(f"unsupported provider adapter for {name}: {provider.adapter}")
        if provider.timeout_seconds <= 0:
            raise ValueError(f"providers.{name}.timeout_seconds must be greater than zero")
        if provider.enabled and (not provider.acp_command or not provider.auth_command):
            raise ValueError(f"enabled provider {name!r} requires acp_command and auth_command")
    unknown_limits = set(scheduler.provider_concurrency) - set(config.providers)
    if unknown_limits:
        raise ValueError(f"provider concurrency configured for unknown providers: {sorted(unknown_limits)}")

    unknown = set(config.repositories) - (set(config.github.allowed_repositories) | set(config.ideas.repositories))
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
        if name in config.ideas.repositories and repository.concurrency_scope == "label":
            raise ValueError(f"ideas repository {name} cannot use label concurrency scope")
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
        preview_dir=_path(service_raw.get("preview_dir"), ServiceConfig.preview_dir),
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

    ideas_raw = raw.get("ideas", {})
    ideas = IdeasConfig(
        repositories=tuple(ideas_raw.get("repositories", [])),
        private_only=bool(ideas_raw.get("private_only", True)),
        spec_path=str(ideas_raw.get("spec_path", "idea/SPEC.md")),
        progress_path=str(ideas_raw.get("progress_path", "idea/PROGRESS.md")),
    )

    scheduler_raw = raw.get("scheduler", {})
    vault_raw = raw.get("vault", {})
    vault = VaultConfig(
        enabled=bool(vault_raw.get("enabled", False)),
        path=_path(vault_raw.get("path"), VaultConfig.path),
        projects_dir=str(vault_raw.get("projects_dir", VaultConfig.projects_dir)),
        owner=str(vault_raw.get("owner", "")),
        provider=str(vault_raw.get("provider", "codex")),
        quiet_seconds=int(vault_raw.get("quiet_seconds", 30)),
        port_start=int(vault_raw.get("port_start", 10000)),
        port_end=int(vault_raw.get("port_end", 10999)),
        manage_checkboxes=bool(vault_raw.get("manage_checkboxes", True)),
    )
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
            permission_mode=str(value.get("permission_mode", "restricted" if name == "antigravity" else "full")),
            **AgentSettings.parse(value).for_provider(name).values(),
        )

    repositories: dict[str, RepositoryConfig] = {}
    for name, value in raw.get("repositories", {}).items():
        repositories[name] = RepositoryConfig(
            concurrency_scope=str(value.get("concurrency_scope", "repository")),
            concurrency_key=str(value.get("concurrency_key", "")),
            concurrency_labels={str(label): str(key) for label, key in value.get("concurrency_labels", {}).items()},
            validation_commands=_validation_commands(value.get("validation_commands")),
            setup_script=str(value.get("setup_script", "")),
            instruction=str(value.get("instruction", "")),
            approval_policy=str(value.get("approval_policy", "safe-code-only")),
            **AgentSettings.parse(value).values(),
        )

    hack_raw = raw.get("hack", {})
    hack = HackConfig(
        enabled=bool(hack_raw.get("enabled", False)),
        repositories=tuple(hack_raw.get("repositories", [])),
        provider=str(hack_raw.get("provider", "codex")),
        max_parallel=int(hack_raw.get("max_parallel", 4)),
        max_tasks=int(hack_raw.get("max_tasks", 100)),
        reserve_slots=int(hack_raw.get("reserve_slots", 1)),
        task_timeout_seconds=int(hack_raw.get("task_timeout_seconds", 1800)),
        fast_gate_timeout_seconds=int(hack_raw.get("fast_gate_timeout_seconds", 300)),
        polish_seconds=int(hack_raw.get("polish_seconds", 300)),
        milestone_every=int(hack_raw.get("milestone_every", 5)),
        publish_ideas=bool(hack_raw.get("publish_ideas", False)),
    )
    config = Config(service, github, scheduler, providers, repositories, ideas, vault, hack)
    _validate_config(config)
    return config
