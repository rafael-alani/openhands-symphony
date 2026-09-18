#!/usr/bin/env bash
# Scoped upgrade for the existing VM; does not reinstall Symphony or provider CLIs.
set -euo pipefail

if [[ ${1:-} != --apply ]]; then
  echo 'Upgrade Canvas to 1.20.0, Agent Server to 1.49.1, automation to 1.13.1, and Node to 24.21.0.'
  echo 'Requires root; refuses active conversations; preserves and verifies rollback state.'
  echo 'Run: sudo bash scripts/upgrade_canvas.sh --apply'
  exit 0
fi
[[ $EUID == 0 ]] || { echo 'Root is required.' >&2; exit 1; }
[[ $(uname -m) == x86_64 ]] || { echo 'This VM upgrade supports x86_64 only.' >&2; exit 1; }
exec 9>/run/lock/openhands-canvas-upgrade.lock
flock -n 9 || { echo 'Another upgrade is running.' >&2; exit 1; }
for path in /etc/openhands-symphony/canvas.env /opt/openhands-symphony/versions.env \
  /etc/systemd/system/openhands-canvas.service /var/lib/openhands-agent/.openhands; do
  [[ -e $path ]] || { echo "Required path missing: $path" >&2; exit 1; }
done
source_path=$(cat /etc/openhands-symphony/source-path)
[[ $source_path == /home/afa/openhands-symphony ]] || { echo 'Unexpected recorded source checkout; review first.' >&2; exit 1; }
for relative in versions.env systemd/openhands-canvas.service packaging/openhands-symphony.nft; do
  cmp -s "$source_path/$relative" "/opt/openhands-symphony/$relative" || {
    echo "Source and deployment differ at $relative; reconcile before upgrading." >&2; exit 1;
  }
done

stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup=/var/backups/openhands-symphony/canvas-upgrade-$stamp
node=/opt/node-v24.21.0-linux-x64
release=/opt/openhands-canvas-1.20.0-$stamp
old_canvas=/opt/openhands-canvas.pre-$stamp
install -d -m 0700 "$backup"
printf '%s\n' "$backup" > /var/backups/openhands-symphony/latest-canvas-upgrade

# Finish downloads before stopping any production service.
curl -fsSLo "$backup/node.tar.xz" https://nodejs.org/dist/v24.21.0/node-v24.21.0-linux-x64.tar.xz
curl -fsSLo "$backup/SHASUMS256.txt" https://nodejs.org/dist/v24.21.0/SHASUMS256.txt
expected=$(awk '$2 == "node-v24.21.0-linux-x64.tar.xz" {print $1}' "$backup/SHASUMS256.txt")
[[ $expected =~ ^[0-9a-f]{64}$ ]]
printf '%s  %s\n' "$expected" "$backup/node.tar.xz" | sha256sum -c -
if [[ ! -e $node ]]; then tar -xJf "$backup/node.tar.xz" -C /opt; fi
[[ $("$node/bin/node" --version) == v24.21.0 ]]
env PATH="$node/bin:/usr/local/bin:/usr/bin:/bin" "$node/bin/npm" install \
  --prefix "$release" --omit=dev --no-audit --no-fund @openhands/agent-canvas@1.20.0 \
  > "$backup/npm-install.log" 2>&1
[[ $("$node/bin/node" "$release/node_modules/@openhands/agent-canvas/bin/agent-canvas.mjs" --version) == 1.20.0 ]]

active_units=()
for unit in openhands-symphony.service openhands-symphony-reconcile.timer openhands-symphony-reconcile.service; do
  if systemctl is-active --quiet "$unit"; then active_units+=("$unit"); fi
done
restore_schedulers() {
  for unit in "${active_units[@]}"; do
    # A oneshot reconciler is recovered by the timer rather than rerun immediately.
    [[ $unit == openhands-symphony-reconcile.service ]] || systemctl start "$unit"
  done
}
changed=false
canvas_stopped=false
on_failure() {
  status=$?
  if [[ $status != 0 ]]; then
    if [[ $changed == false ]]; then
      if [[ $canvas_stopped == true ]]; then systemctl start openhands-canvas.service; fi
      restore_schedulers
    else
      systemctl stop openhands-canvas.service
      echo "Upgrade failed; services remain paused. Restore with: sudo bash $backup/rollback.sh" >&2
    fi
  fi
}
# Never print the API key or conversation contents. Refuse unknown response shapes.
assert_idle() {
python3 - <<'PY'
import json, pathlib, urllib.parse, urllib.request
key = pathlib.Path('/etc/openhands-symphony/canvas.env').read_text().strip().split('=', 1)[1]
page = None
while True:
    query = urllib.parse.urlencode({'limit': 100, **({'page_id': page} if page else {})})
    req = urllib.request.Request('http://127.0.0.1:8000/api/conversations/search?' + query,
                                headers={'X-Session-API-Key': key})
    with urllib.request.urlopen(req, timeout=30) as response: payload = json.load(response)
    assert isinstance(payload.get('items'), list), 'Unknown conversation-list response; refusing upgrade'
    idle = {'idle', 'finished', 'paused', 'stopped', 'error', 'completed', 'waiting_for_user'}
    assert all(str(item.get('execution_status', '')).lower() in idle for item in payload['items']), \
        'A conversation is active or has an unknown status; retry after it stops'
    page = payload.get('next_page_id')
    if not page: break
print('Conversation idle check passed.')
PY
}
assert_idle
trap on_failure EXIT
systemctl stop openhands-symphony-reconcile.timer openhands-symphony-reconcile.service openhands-symphony.service
assert_idle
systemctl stop openhands-canvas.service
canvas_stopped=true

# Only Canvas state and the exact configuration changed below are in this archive.
# Provider credentials, browser profiles, worktrees and Symphony code stay untouched.
paths=(var/lib/openhands-agent/.openhands etc/openhands-symphony
  etc/systemd/system/openhands-canvas.service opt/openhands-symphony/versions.env
  opt/openhands-symphony/systemd/openhands-canvas.service
  opt/openhands-symphony/packaging/openhands-symphony.nft
  home/afa/openhands-symphony/versions.env
  home/afa/openhands-symphony/systemd/openhands-canvas.service
  home/afa/openhands-symphony/packaging/openhands-symphony.nft)
for binary in node npm npx corepack; do
  [[ ! -L /usr/local/bin/$binary && ! -e /usr/local/bin/$binary ]] || paths+=("usr/local/bin/$binary")
done
required=$(du -sb /var/lib/openhands-agent/.openhands | awk '{print $1}')
available=$(df -B1 --output=avail "$backup" | tail -1)
(( available > required * 3 + 1073741824 )) || { echo 'Insufficient rollback space.' >&2; exit 1; }
tar --xattrs --acls -C / -czf "$backup/state.tgz" "${paths[@]}"
tar --xattrs --acls -C / -dzf "$backup/state.tgz"
install -d -m 0700 "$backup/restore-check"
tar --xattrs --acls -C "$backup/restore-check" -xzf "$backup/state.tgz"
tar --xattrs --acls -C "$backup/restore-check" -dzf "$backup/state.tgz"
sha256sum "$backup/state.tgz" > "$backup/state.sha256"

cat > "$backup/rollback.sh" <<ROLLBACK
#!/usr/bin/env bash
set -euo pipefail
[[ \$EUID == 0 ]]
sha256sum -c '$backup/state.sha256'
systemctl stop openhands-symphony-reconcile.timer openhands-symphony-reconcile.service openhands-symphony.service openhands-canvas.service
failed=\$(date -u +%Y%m%dT%H%M%SZ)
mv /var/lib/openhands-agent/.openhands /var/lib/openhands-agent/.openhands.failed-\$failed
if [[ -L /opt/openhands-canvas ]]; then mv /opt/openhands-canvas /opt/openhands-canvas.failed-\$failed; fi
if [[ -d '$old_canvas' ]]; then mv '$old_canvas' /opt/openhands-canvas; fi
tar --xattrs --acls -C / -xzf '$backup/state.tgz'
systemctl daemon-reload
{ echo 'flush table inet openhands_symphony'; cat /opt/openhands-symphony/packaging/openhands-symphony.nft; } | nft -f -
systemctl start openhands-canvas.service
echo 'Previous Canvas and state restored. Check its UI before restarting openhands-symphony.service and openhands-symphony-reconcile.timer.'
ROLLBACK
chmod 0700 "$backup/rollback.sh"
bash -n "$backup/rollback.sh"
echo "Verified archive and extracted restore retained at $backup"

changed=true
mv /opt/openhands-canvas "$old_canvas"
ln -s "$release" /opt/openhands-canvas
for binary in node npm npx; do ln -sfn "$node/bin/$binary" "/usr/local/bin/$binary"; done
python3 - <<'PY'
from pathlib import Path
pins = {'NODE_VERSION': '24.21.0', 'AGENT_CANVAS_VERSION': '1.20.0',
        'AGENT_SERVER_VERSION': '1.49.1', 'OPENHANDS_AUTOMATION_VERSION': '1.13.1'}
for root in ('/opt/openhands-symphony', '/home/afa/openhands-symphony'):
    p = Path(root) / 'versions.env'
    p.write_text('\n'.join(k + '=' + pins[k] if (k := line.split('=', 1)[0]) in pins else line
                           for line in p.read_text().splitlines()) + '\n')
for p in (Path('/etc/systemd/system/openhands-canvas.service'),
          Path('/opt/openhands-symphony/systemd/openhands-canvas.service'),
          Path('/home/afa/openhands-symphony/systemd/openhands-canvas.service')):
    text = p.read_text()
    import re
    text = re.sub(r'(?m)^Environment=OH_AGENT_SERVER_VERSION=.*$', 'Environment=OH_AGENT_SERVER_VERSION=1.49.1', text)
    text = re.sub(r'(?m)^Environment=OH_AUTOMATION_VERSION=.*$', 'Environment=OH_AUTOMATION_VERSION=1.13.1', text)
    p.write_text(text)
for root in ('/opt/openhands-symphony', '/home/afa/openhands-symphony'):
    p = Path(root) / 'packaging/openhands-symphony.nft'
    text = p.read_text()
    assert '{ 8000, 8787, 9222 }' in text, 'Unexpected firewall; requires review'
    p.write_text(text.replace('{ 8000, 8787, 9222 }', '{ 3001, 8000, 8787, 9222, 18000, 18001, 19000 }'))
PY
# Atomic replacement of this service's table; SSH and the general firewall are untouched.
{ echo 'flush table inet openhands_symphony'; cat /opt/openhands-symphony/packaging/openhands-symphony.nft; } | nft -f -
systemctl daemon-reload
systemctl start openhands-canvas.service
python3 - <<'PY'
import json, pathlib, time, urllib.request
key = pathlib.Path('/etc/openhands-symphony/canvas.env').read_text().strip().split('=', 1)[1]
for attempt in range(120):
    try:
        req = urllib.request.Request('http://127.0.0.1:8000/server_info', headers={'X-Session-API-Key': key})
        with urllib.request.urlopen(req, timeout=5) as response: info = json.load(response)
        assert info['version'] == '1.49.1'
        with urllib.request.urlopen('http://127.0.0.1:8000/', timeout=5) as response: assert response.status == 200
        req = urllib.request.Request('http://127.0.0.1:8000/api/automation/health', headers={'X-Session-API-Key': key})
        with urllib.request.urlopen(req, timeout=5) as response: assert response.status == 200
        print('Canvas UI, Agent Server 1.49.1, and automation health verified.')
        break
    except Exception:
        time.sleep(3)
else:
    raise SystemExit('New Canvas failed readiness; use the retained rollback script.')
PY
restore_schedulers
trap - EXIT
echo "OpenHands 1.20.0 is running. Rollback: sudo bash $backup/rollback.sh"
echo 'The VM source checkout also contains the three version/service/firewall edits; preserve them when reconciling Git.'
