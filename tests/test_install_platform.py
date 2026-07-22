from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

PLATFORM_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_platform.sh"
INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"
CHROMIUM_APPARMOR = Path(__file__).resolve().parents[1] / "packaging" / "openhands-symphony-chromium.apparmor"
BROWSER_UNIT = Path(__file__).resolve().parents[1] / "systemd" / "openhands-browser.service"
BROWSER_LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "launch_headless_browser.sh"


def _check_platform(distribution_id: str, version: str, pretty_name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; symphony_require_supported_platform "$2" "$3" "$4"',
            "platform-test",
            str(PLATFORM_SCRIPT),
            distribution_id,
            version,
            pretty_name,
        ],
        check=False,
        text=True,
        capture_output=True,
    )


@pytest.mark.parametrize("version", ["24.04", "26.04"])
def test_supported_ubuntu_lts_versions(version: str) -> None:
    result = _check_platform("ubuntu", version, f"Ubuntu {version} LTS")

    assert result.returncode == 0
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("distribution_id", "version", "pretty_name"),
    [
        ("ubuntu", "25.10", "Ubuntu 25.10"),
        ("ubuntu", "28.04", "Ubuntu 28.04 LTS"),
        ("debian", "13", "Debian GNU/Linux 13"),
    ],
)
def test_unsupported_platforms_are_rejected(distribution_id: str, version: str, pretty_name: str) -> None:
    result = _check_platform(distribution_id, version, pretty_name)

    assert result.returncode == 1
    assert "Ubuntu 24.04 or 26.04 LTS is required" in result.stderr
    assert pretty_name in result.stderr


def test_installer_uses_release_neutral_python_and_playwright_dependencies() -> None:
    installer = INSTALLER.read_text()

    assert "python3.12" not in installer
    assert "qemu-guest-agent" in installer
    assert 'install -d -o "${SERVICE_USER}" -g "${SHARED_GROUP}" -m 2710 "${STATE_DIR}/workspaces"' in installer
    assert "UV_PYTHON_INSTALL_DIR=/opt/uv-python" in installer
    assert "UV_MANAGED_PYTHON=true" in installer
    assert 'uv python install "${PYTHON_VERSION}"' in installer
    assert installer.count('--python "${PYTHON_VERSION}"') == 3
    assert "uv tool install --force --locked" not in installer
    assert "UV_PROJECT_ENVIRONMENT=/opt/openhands-symphony-tool" in installer
    assert (
        'uv sync --locked --no-dev --no-editable --reinstall-package "${PROJECT_NAME}" --project "${INSTALL_DIR}"'
        in installer
    )
    assert 'cmp -s "${source_file}" "${INSTALLED_SYMPHONY_DIR}/${relative_file}"' in installer
    assert "installed Symphony package is stale" in installer
    assert '--with-executables-from "browser-harness==${BROWSER_HARNESS_VERSION}"' in installer
    assert "playwright install chromium --with-deps --no-shell" in installer
    assert "libatk1.0-0 " not in installer
    assert "libcups2 " not in installer
    assert 'if [[ ! -x "/usr/local/bin/${installed_command}" ]]' in installer
    assert "Codex CLI: /usr/local/bin/codex" in installer
    assert "Do not route a real issue until every required doctor row reports PASS." in installer
    assert "apparmor_parser -r /etc/apparmor.d/openhands-symphony-chromium" in installer


def test_chromium_apparmor_profile_narrowly_allows_the_pinned_browser_tree() -> None:
    profile = CHROMIUM_APPARMOR.read_text()

    assert "/opt/browser-use/chromium/**/chrome" in profile
    assert "userns," in profile
    assert "/**" not in profile.replace("/opt/browser-use/chromium/**/chrome", "")


def test_browser_crashpad_state_stays_in_the_writable_private_browser_home() -> None:
    installer = INSTALLER.read_text()
    unit = BROWSER_UNIT.read_text()
    launcher = BROWSER_LAUNCHER.read_text()

    for name in ("xdg-config", "xdg-cache", "xdg-data"):
        path = f"/var/lib/openhands-agent/browser/{name}"
        assert path in unit
        assert path in launcher
        assert f'"${{AGENT_STATE_DIR}}/browser/{name}"' in installer
    assert "ReadWritePaths=/var/lib/openhands-agent/browser" in unit
    assert "--disable-breakpad" in launcher
