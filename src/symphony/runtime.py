from __future__ import annotations

from pathlib import Path

from .config import Config, load_config
from .coordinator import Coordinator
from .github import GhCLIBackend
from .providers.base import ProviderAdapter
from .providers.openhands import OpenHandsACPProvider
from .store import Store


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
        )
    return providers


def build_coordinator(config_path: str | Path | None = None) -> tuple[Config, Store, Coordinator]:
    config = load_config(config_path)
    config.service.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(config.service.state_dir / "state.db")
    github = GhCLIBackend(
        config.github.allowed_repositories,
        private_only=config.github.private_only,
        bot_login=config.github.bot_login,
    )
    coordinator = Coordinator(config, store, github, build_providers(config))
    return config, store, coordinator
