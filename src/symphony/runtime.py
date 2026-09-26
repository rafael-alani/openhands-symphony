from __future__ import annotations

from pathlib import Path

from .config import Config, load_config
from .coordinator import Coordinator
from .execution import ProviderSlots
from .github import GhCLIBackend
from .ideas_coordinator import IdeasCoordinator
from .ideas_github import GhIdeasBackend
from .providers.base import ProviderAdapter
from .providers.openhands import OpenHandsACPProvider
from .store import Store


def validate_operational_config(config: Config) -> None:
    placeholders = [
        repository
        for repository in (*config.github.allowed_repositories, *config.ideas.repositories, *config.hack.repositories)
        if "CHANGE_ME" in repository
    ]
    if placeholders:
        raise ValueError(
            "replace CHANGE_ME/CHANGE_ME in both github.allowed_repositories and the matching "
            '[repositories."owner/repository"] section of /etc/openhands-symphony/config.toml'
        )


def build_providers(config: Config) -> dict[str, ProviderAdapter]:
    providers: dict[str, ProviderAdapter] = {}
    for name, provider_config in config.providers.items():
        if not provider_config.enabled:
            continue
        if provider_config.adapter != "openhands-acp":
            raise ValueError(f"unsupported provider adapter for {name}: {provider_config.adapter}")
        providers[name] = OpenHandsACPProvider(
            name,
            config.service.agent_server_url,
            provider_config.acp_command,
            provider_config.auth_command,
            api_key_file=config.service.agent_server_api_key_file,
            auth_marker_file=provider_config.auth_marker_file,
            permission_mode=provider_config.permission_mode,
            settings=config.agent_settings(name),
        )
    return providers


def build_coordinator(config_path: str | Path | None = None) -> tuple[Config, Store, Coordinator]:
    config = load_config(config_path)
    validate_operational_config(config)
    config.service.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(config.service.state_dir / "state.db")
    github = GhCLIBackend(
        config.github.allowed_repositories,
        private_only=config.github.private_only,
        bot_login=config.github.bot_login,
    )
    providers = build_providers(config)
    provider_slots = ProviderSlots(config.scheduler.provider_concurrency, set(providers))
    coordinator = Coordinator(config, store, github, providers, provider_slots)
    coordinator.ideas = IdeasCoordinator(
        config,
        store,
        GhIdeasBackend(config.ideas.repositories, private_only=config.ideas.private_only),
        providers,
        provider_slots,
    )
    from .hack_coordinator import GhHackBackend, HackCoordinator

    coordinator.hack = HackCoordinator(
        config, store, GhHackBackend(config.hack.repositories), providers, provider_slots,
    )
    if config.vault.enabled:
        from .vault import VaultBridge

        coordinator.vault = VaultBridge(config, store)
        coordinator.apply_vault_config()
        coordinator.ideas.vault = coordinator.vault
        coordinator.hack.vault = coordinator.vault
    return coordinator.config, store, coordinator
