#!/usr/bin/env bash
# Optional VM-local vault transport. Pairing is a separate, explicit operation.
set -euo pipefail

[[ ${EUID} -eq 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
command -v agentctl >/dev/null || { echo 'Install Symphony first.' >&2; exit 1; }

install -d -m 0755 /etc/apt/keyrings
curl --fail --silent --show-error --location https://syncthing.net/release-key.gpg \
  -o /etc/apt/keyrings/syncthing-archive-keyring.gpg
chmod 0644 /etc/apt/keyrings/syncthing-archive-keyring.gpg
echo 'deb [signed-by=/etc/apt/keyrings/syncthing-archive-keyring.gpg] https://apt.syncthing.net/ syncthing stable-v2' \
  > /etc/apt/sources.list.d/syncthing.list
apt-get update
apt-get install -y --no-install-recommends syncthing

getent group symphony-vault >/dev/null || groupadd --system symphony-vault
id symphony-sync >/dev/null 2>&1 || useradd --system --create-home \
  --home-dir /var/lib/symphony-sync --gid symphony-vault --shell /usr/sbin/nologin symphony-sync
install -d -o symphony-sync -g symphony-vault -m 0700 /var/lib/symphony-sync
if [[ ! -f /var/lib/symphony-sync/config.xml ]]; then
  runuser -u symphony-sync -- env STNODEFAULTFOLDER=1 \
    syncthing generate --home=/var/lib/symphony-sync --no-port-probing
  # The VM uses an explicit LAN peer. Do not advertise the device globally,
  # request router mappings, or use third-party relays on first startup.
  python3 - <<'PY'
from pathlib import Path
import xml.etree.ElementTree as ET
p = Path('/var/lib/symphony-sync/config.xml')
t = ET.parse(p)
r = t.getroot()
for f in r.findall('folder'):
    r.remove(f)
o = r.find('options')
for name in ('globalAnnounceEnabled', 'localAnnounceEnabled', 'relaysEnabled', 'natEnabled', 'startBrowser'):
    item = o.find(name)
    if item is None:
        item = ET.SubElement(o, name)
    item.text = 'false'
for item in o.findall('listenAddress'):
    o.remove(item)
ET.SubElement(o, 'listenAddress').text = 'tcp://0.0.0.0:22000'
t.write(p, encoding='utf-8', xml_declaration=True)
PY
fi

# configure_vault preserves existing directory ownership. It creates a new
# enabled vault with the shared transport group, and excludes workers from it.
/opt/openhands-symphony-tool/bin/python /opt/openhands-symphony/scripts/configure_vault.py
VAULT_PATH=$(/opt/openhands-symphony-tool/bin/python - <<'PY'
from symphony.config import load_config
c = load_config('/etc/openhands-symphony/config.toml')
if not c.vault.enabled:
    raise SystemExit('Enable [vault] before installing its Syncthing transport.')
print(c.vault.path)
PY
)
runuser -u symphony-sync -- test -w "${VAULT_PATH}" || {
  echo 'Existing vault is not writable by symphony-sync; arrange scoped group access first.' >&2
  exit 1
}

/opt/openhands-symphony-tool/bin/python - "${VAULT_PATH}" <<'PY'
import json
import sys
from pathlib import Path
path = json.dumps(sys.argv[1].replace('%', '%%'))
Path('/etc/systemd/system/symphony-syncthing.service').write_text('''[Unit]
Description=Syncthing transport for the Symphony Obsidian vault
After=network-online.target
Wants=network-online.target

[Service]
User=symphony-sync
Group=symphony-vault
Environment=STNODEFAULTFOLDER=1
ExecStart=/usr/bin/syncthing serve --no-browser --no-restart --no-upgrade --home=/var/lib/symphony-sync --gui-address=127.0.0.1:8384
Restart=on-failure
RestartSec=5
UMask=0007
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
CapabilityBoundingSet=
ReadWritePaths=/var/lib/symphony-sync ''' + path + '''

[Install]
WantedBy=multi-user.target
''')
PY
systemctl daemon-reload
systemctl enable --now symphony-syncthing.service
syncthing --version
echo 'Transport installed. Pair only the intended folder; set ignorePerms=true on the VM copy.'
echo 'The control API listens on 127.0.0.1:8384. Existing folders/devices were not changed.'
