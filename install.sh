#!/usr/bin/env bash
set -euo pipefail

PROJECT_NAME="openhands-symphony"
SERVICE_USER="openhands-symphony"
AGENT_USER="openhands-agent"
VALIDATOR_USER="openhands-validator"
SHARED_GROUP="openhands-agents"
AUTH_GROUP="openhands-operators"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/openhands-symphony"
CONFIG_DIR="/etc/openhands-symphony"
STATE_DIR="/var/lib/openhands-symphony"
AGENT_STATE_DIR="/var/lib/openhands-agent"
VALIDATOR_STATE_DIR="/var/lib/openhands-validator"
AUTH_STATUS_DIR="/var/lib/openhands-auth-status"
LOG_DIR="/var/log/openhands-symphony"
STACK_WAS_ACTIVE=false

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer as root: sudo ./install.sh" >&2
  exit 1
fi

if [[ ! -r /etc/os-release ]]; then
  echo "Unsupported distribution: /etc/os-release is missing" >&2
  exit 1
fi
. /etc/os-release
if [[ ${ID:-} != "ubuntu" || ${VERSION_ID:-} != "24.04" ]]; then
  echo "Unsupported distribution: Ubuntu 24.04 LTS is required; found ${PRETTY_NAME:-unknown}" >&2
  exit 1
fi

# shellcheck source=versions.env
. "${SOURCE_DIR}/versions.env"
export DEBIAN_FRONTEND=noninteractive

if systemctl is-active --quiet openhands-symphony.target 2>/dev/null; then
  STACK_WAS_ACTIVE=true
  systemctl stop openhands-symphony.target
fi

apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl git gnupg jq nftables openssl rsync sqlite3 xz-utils build-essential \
  sudo \
  python3.12 python3.12-venv libsecret-1-0 dbus-user-session dbus-x11 gnome-keyring \
  libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
  libgbm1 libasound2t64 libpango-1.0-0 libcairo2 fonts-liberation

install -d -m 0755 /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/githubcli-archive-keyring.gpg ]]; then
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
  chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
fi
ARCH="$(dpkg --print-architecture)"
echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  > /etc/apt/sources.list.d/github-cli.list
apt-get update
apt-get install -y --no-install-recommends gh

case "${ARCH}" in
  amd64) NODE_ARCH="x64" ;;
  arm64) NODE_ARCH="arm64" ;;
  *) echo "Unsupported CPU architecture for pinned Node.js: ${ARCH}" >&2; exit 1 ;;
esac
NODE_PREFIX="/opt/node-v${NODE_VERSION}-linux-${NODE_ARCH}"
if [[ ! -x "${NODE_PREFIX}/bin/node" ]]; then
  TMP_NODE="$(mktemp -d)"
  curl -fsSLO --output-dir "${TMP_NODE}" "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"
  curl -fsSLo "${TMP_NODE}/SHASUMS256.txt" "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt"
  (
    cd "${TMP_NODE}"
    grep " node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz$" SHASUMS256.txt | sha256sum -c -
  )
  tar -xJf "${TMP_NODE}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" -C /opt
  rm -rf "${TMP_NODE}"
fi
for binary in node npm npx corepack; do
  ln -sfn "${NODE_PREFIX}/bin/${binary}" "/usr/local/bin/${binary}"
done

if ! command -v uv >/dev/null || [[ "$(uv --version)" != "uv ${UV_VERSION}" ]]; then
  curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | env UV_INSTALL_DIR=/usr/local/bin sh
fi

if ! getent group "${SHARED_GROUP}" >/dev/null; then
  groupadd --system "${SHARED_GROUP}"
fi
if ! getent group "${AUTH_GROUP}" >/dev/null; then
  groupadd --system "${AUTH_GROUP}"
fi
if ! getent group "${SERVICE_USER}" >/dev/null; then
  groupadd --system "${SERVICE_USER}"
fi
if ! getent group "${AGENT_USER}" >/dev/null; then
  groupadd --system "${AGENT_USER}"
fi
if ! getent group "${VALIDATOR_USER}" >/dev/null; then
  groupadd --system "${VALIDATOR_USER}"
fi
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${STATE_DIR}" --gid "${SERVICE_USER}" --groups "${SHARED_GROUP},${AUTH_GROUP}" --shell /bin/bash "${SERVICE_USER}"
fi
if ! id "${AGENT_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${AGENT_STATE_DIR}" --gid "${AGENT_USER}" --groups "${SHARED_GROUP}" --shell /bin/bash "${AGENT_USER}"
fi
if ! id "${VALIDATOR_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${VALIDATOR_STATE_DIR}" --gid "${VALIDATOR_USER}" --groups "${SHARED_GROUP}" --shell /usr/sbin/nologin "${VALIDATOR_USER}"
fi
usermod -g "${SERVICE_USER}" -G "${SHARED_GROUP},${AUTH_GROUP}" "${SERVICE_USER}"
usermod -g "${AGENT_USER}" -G "${SHARED_GROUP}" "${AGENT_USER}"
usermod -g "${VALIDATOR_USER}" -G "${SHARED_GROUP}" "${VALIDATOR_USER}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0700 \
  "${STATE_DIR}" "${STATE_DIR}/reports" "${STATE_DIR}/github" "${LOG_DIR}"
install -d -o "${SERVICE_USER}" -g "${SHARED_GROUP}" -m 2770 "${STATE_DIR}/workspaces"
install -d -o "${AGENT_USER}" -g "${AGENT_USER}" -m 0700 \
  "${AGENT_STATE_DIR}" "${AGENT_STATE_DIR}/browser" \
  "${AGENT_STATE_DIR}/browser/chromium-profile" "${AGENT_STATE_DIR}/browser/agent-workspace"
install -d -o "${VALIDATOR_USER}" -g "${VALIDATOR_USER}" -m 0700 "${VALIDATOR_STATE_DIR}"
install -d -o "${AGENT_USER}" -g "${AUTH_GROUP}" -m 2750 "${AUTH_STATUS_DIR}"
install -d -o root -g "${SERVICE_USER}" -m 0751 "${CONFIG_DIR}"

install -d -m 0755 "${INSTALL_DIR}"
if [[ "${SOURCE_DIR}" != "${INSTALL_DIR}" ]]; then
  rsync -a --delete \
    --exclude .git --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache \
    "${SOURCE_DIR}/" "${INSTALL_DIR}/"
fi
chmod 0755 "${INSTALL_DIR}"/scripts/*.sh "${INSTALL_DIR}"/scripts/*.py

install -d -m 0755 /opt/openhands-canvas /opt/openhands-acp /opt/provider-clis \
  /opt/antigravity-acp /opt/antigravity-cli /opt/browser-use /opt/browser-use/chromium \
  /opt/openhands-symphony-tool
npm install --prefix /opt/openhands-canvas --omit=dev --no-audit --no-fund \
  "@openhands/agent-canvas@${AGENT_CANVAS_VERSION}"
npm install --prefix /opt/openhands-acp --omit=dev --no-audit --no-fund \
  "@agentclientprotocol/claude-agent-acp@${CLAUDE_ACP_VERSION}" \
  "@agentclientprotocol/codex-acp@${CODEX_ACP_VERSION}"
npm install --prefix /opt/provider-clis --omit=dev --no-audit --no-fund \
  "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
  "@openai/codex@${CODEX_VERSION}"

env UV_TOOL_DIR=/opt/openhands-symphony-tool/tools UV_TOOL_BIN_DIR=/opt/openhands-symphony-tool/bin \
  uv tool install --force --locked --python python3.12 "${INSTALL_DIR}"
env UV_TOOL_DIR=/opt/browser-use/tools UV_TOOL_BIN_DIR=/opt/browser-use/bin \
  uv tool install --force --python python3.12 \
  --with "browser-harness==${BROWSER_HARNESS_VERSION}" "browser-use==${BROWSER_USE_VERSION}"

env PLAYWRIGHT_BROWSERS_PATH=/opt/browser-use/chromium \
  uvx --from "playwright==${PLAYWRIGHT_VERSION}" playwright install chromium --with-deps --no-shell
chmod -R a+rX /opt/browser-use/chromium

uv venv --clear --python python3.12 /opt/antigravity-acp
uv pip install --python /opt/antigravity-acp/bin/python "agent-client-protocol==${ACP_PYTHON_VERSION}"

case "${ARCH}" in
  amd64)
    ANTIGRAVITY_URL="${ANTIGRAVITY_AMD64_URL}"
    ANTIGRAVITY_SHA512="${ANTIGRAVITY_AMD64_SHA512}"
    ;;
  arm64)
    ANTIGRAVITY_URL="${ANTIGRAVITY_ARM64_URL}"
    ANTIGRAVITY_SHA512="${ANTIGRAVITY_ARM64_SHA512}"
    ;;
esac
AGY_PATH="/opt/antigravity-cli/agy"
if [[ ! -x "${AGY_PATH}" || "$(env AGY_CLI_DISABLE_AUTO_UPDATE=true "${AGY_PATH}" --version 2>/dev/null || true)" != "${ANTIGRAVITY_VERSION}" ]]; then
  TMP_AGY="$(mktemp -d)"
  curl -fsSL "${ANTIGRAVITY_URL}" -o "${TMP_AGY}/agy.tar.gz"
  echo "${ANTIGRAVITY_SHA512}  ${TMP_AGY}/agy.tar.gz" | sha512sum -c -
  tar -xzf "${TMP_AGY}/agy.tar.gz" -C "${TMP_AGY}" antigravity
  install -o root -g root -m 0755 "${TMP_AGY}/antigravity" "${AGY_PATH}"
  rm -rf "${TMP_AGY}"
fi

ln -sfn /opt/openhands-symphony-tool/bin/agentctl /usr/local/bin/agentctl
ln -sfn /opt/openhands-symphony-tool/bin/openhands-symphony /usr/local/bin/openhands-symphony
ln -sfn /opt/provider-clis/node_modules/.bin/claude /usr/local/bin/claude
ln -sfn /opt/provider-clis/node_modules/.bin/codex /usr/local/bin/codex
ln -sfn /opt/browser-use/bin/browser-use /usr/local/bin/browser-use
ln -sfn /opt/browser-use/bin/browser-harness /usr/local/bin/browser-harness
if [[ -x "${AGY_PATH}" ]]; then
  ln -sfn "${AGY_PATH}" /usr/local/bin/agy
fi

if [[ ! -f "${CONFIG_DIR}/config.toml" ]]; then
  install -o root -g "${SERVICE_USER}" -m 0640 "${INSTALL_DIR}/examples/config.toml" "${CONFIG_DIR}/config.toml"
fi
if [[ ! -f "${CONFIG_DIR}/webhook-secret" ]]; then
  umask 077
  openssl rand -hex 32 > "${CONFIG_DIR}/webhook-secret"
  chown root:"${SERVICE_USER}" "${CONFIG_DIR}/webhook-secret"
  chmod 0640 "${CONFIG_DIR}/webhook-secret"
fi
if [[ ! -f "${CONFIG_DIR}/canvas.env" ]]; then
  umask 077
  echo "LOCAL_BACKEND_API_KEY=$(openssl rand -hex 32)" > "${CONFIG_DIR}/canvas.env"
fi
chown root:"${SERVICE_USER}" "${CONFIG_DIR}/canvas.env"
chmod 0640 "${CONFIG_DIR}/canvas.env"
install -o root -g root -m 0440 \
  "${INSTALL_DIR}/packaging/openhands-symphony-validator.sudoers" \
  /etc/sudoers.d/openhands-symphony-validator
visudo -cf /etc/sudoers.d/openhands-symphony-validator >/dev/null
if [[ "${SOURCE_DIR}" != "${INSTALL_DIR}" ]]; then
  echo "${SOURCE_DIR}" > "${CONFIG_DIR}/source-path"
  chown root:"${SERVICE_USER}" "${CONFIG_DIR}/source-path"
  chmod 0640 "${CONFIG_DIR}/source-path"
fi

for unit in "${INSTALL_DIR}"/systemd/*; do
  install -m 0644 "${unit}" "/etc/systemd/system/$(basename "${unit}")"
done
systemctl daemon-reload
systemctl enable openhands-symphony.target
if [[ "${STACK_WAS_ACTIVE}" == true ]]; then
  systemctl start openhands-symphony.target
fi

echo
echo "Installation complete. Configuration and existing credentials were preserved."
echo "1. Edit ${CONFIG_DIR}/config.toml and replace CHANGE_ME/CHANGE_ME."
echo "2. Start the private worker keyring, then run these interactive commands exactly:"
echo "   sudo systemctl start openhands-agent-keyring.service"
echo "   sudo -iu ${SERVICE_USER} agentctl auth github"
echo "   sudo -iu ${AGENT_USER} agentctl auth claude"
echo "   sudo -iu ${AGENT_USER} agentctl auth codex"
echo "   sudo -iu ${AGENT_USER} agentctl auth antigravity"
echo "3. Then create labels, start, and verify:"
echo "   sudo -iu ${SERVICE_USER} agentctl labels"
echo "   sudo agentctl start"
echo "   sudo -iu ${SERVICE_USER} agentctl doctor"
echo "   sudo -iu ${SERVICE_USER} agentctl status"
echo "Non-loopback ingress to Canvas/webhook/CDP is blocked by nftables. Use an SSH tunnel by default."
