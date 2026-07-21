from __future__ import annotations

from symphony.cli import _antigravity_cpu_error


def test_antigravity_cpu_preflight_rejects_x86_vm_without_pclmulqdq() -> None:
    error = _antigravity_cpu_error(machine="x86_64", cpuinfo="flags : sse4_2 aes")

    assert error is not None
    assert "does not expose PCLMULQDQ" in error


def test_antigravity_cpu_preflight_accepts_pclmulqdq() -> None:
    assert _antigravity_cpu_error(machine="x86_64", cpuinfo="flags : sse4_2 pclmulqdq aes") is None
