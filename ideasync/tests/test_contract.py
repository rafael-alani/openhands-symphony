from __future__ import annotations

from pathlib import Path

import pytest

from ideasync.contract import read_preview_contract
from ideasync.errors import ContractError

RUNTIME = b'''provider = "codex"

[preview]
start = ["python3", "-m", "http.server", "{port}"]
port = 4317
health_path = "/health"
startup_timeout_seconds = 20
'''


@pytest.mark.parametrize(
    ("original", "replacement", "message"),
    [
        (b'provider = "codex"', b'provider = "bad provider"', "safe non-empty name"),
        (b'health_path = "/health"', b'health_path = "//other-host/health"', "without a query or fragment"),
        (b"startup_timeout_seconds = 20", b"startup_timeout_seconds = 1.5", "positive integer"),
    ],
)
def test_preview_contract_rejects_values_symphony_rejects(
    tmp_path: Path,
    original: bytes,
    replacement: bytes,
    message: str,
) -> None:
    contract = tmp_path / "idea.toml"
    contract.write_bytes(RUNTIME.replace(original, replacement))

    with pytest.raises(ContractError, match=message):
        read_preview_contract(contract)
