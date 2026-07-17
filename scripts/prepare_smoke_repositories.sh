#!/usr/bin/env bash
set -euo pipefail
export GH_CONFIG_DIR="${GH_CONFIG_DIR:-/var/lib/openhands-symphony/github}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUFFIX="${SYMPHONY_SMOKE_SUFFIX:-20260716}"
OWNER="$(gh api user --jq .login)"

if [[ ! "${OWNER}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "Authenticated GitHub login has an unsafe repository-name form" >&2
  exit 1
fi

create_repository() {
  local name="$1"
  local full="${OWNER}/${name}"
  if gh repo view "${full}" --json nameWithOwner,isPrivate >/dev/null 2>&1; then
    local private
    private="$(gh repo view "${full}" --json isPrivate --jq .isPrivate)"
    if [[ "${private}" != "true" ]]; then
      echo "Refusing existing non-private repository: ${full}" >&2
      exit 1
    fi
    echo "preserved existing private repository ${full}"
    return
  fi

  local staging
  staging="$(mktemp -d)"
  trap 'rm -rf "${staging}"' RETURN
  rsync -a "${ROOT_DIR}/smoke/fixture/" "${staging}/"
  git -C "${staging}" init -b main >/dev/null
  git -C "${staging}" add --all
  git -C "${staging}" -c user.name="OpenHands Symphony Smoke" \
    -c user.email="openhands-symphony@localhost" commit -m "Initial smoke fixture" >/dev/null
  gh repo create "${full}" --private --source "${staging}" --remote origin --push >/dev/null
  echo "created private repository ${full}"
  rm -rf "${staging}"
  trap - RETURN
}

PRIMARY="openhands-symphony-smoke-${SUFFIX}"
PEER="openhands-symphony-smoke-peer-${SUFFIX}"
create_repository "${PRIMARY}"
create_repository "${PEER}"

cat <<EOF

Smoke repositories (they will not be deleted):
  ${OWNER}/${PRIMARY}
  ${OWNER}/${PEER}

Add both to github.allowed_repositories and add this validation command to both repository sections:
  ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]
EOF
