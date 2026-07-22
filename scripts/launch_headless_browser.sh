#!/usr/bin/env bash
set -euo pipefail

BROWSER_ROOT="${PLAYWRIGHT_BROWSERS_PATH:-/opt/browser-use/chromium}"
PROFILE="${SYMPHONY_BROWSER_PROFILE:-/var/lib/openhands-agent/browser/chromium-profile}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-/var/lib/openhands-agent/browser/xdg-config}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/var/lib/openhands-agent/browser/xdg-cache}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-/var/lib/openhands-agent/browser/xdg-data}"

mkdir -p "${PROFILE}" "${XDG_CONFIG_HOME}" "${XDG_CACHE_HOME}" "${XDG_DATA_HOME}"

CHROME="$(find "${BROWSER_ROOT}" -type f -name chrome -path '*/chrome-linux*/chrome' -perm -111 -print -quit)"
if [[ -z "${CHROME}" ]]; then
  echo "Pinned Playwright Chromium executable was not found below ${BROWSER_ROOT}" >&2
  exit 1
fi

exec "${CHROME}" \
  --headless=new \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="${PROFILE}" \
  --no-first-run \
  --no-default-browser-check \
  --disable-breakpad \
  --disable-background-networking \
  --disable-dev-shm-usage \
  about:blank
