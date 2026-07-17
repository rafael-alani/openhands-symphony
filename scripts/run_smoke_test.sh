#!/usr/bin/env bash
set -euo pipefail
export GH_CONFIG_DIR="${GH_CONFIG_DIR:-/var/lib/openhands-symphony/github}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sudo -v
orchestrator() {
  sudo -u openhands-symphony -- env \
    HOME=/var/lib/openhands-symphony \
    GH_CONFIG_DIR=/var/lib/openhands-symphony/github \
    SYMPHONY_CONFIG=/etc/openhands-symphony/config.toml \
    "$@"
}
gho() { orchestrator gh "$@"; }
agentctl_o() { orchestrator agentctl "$@"; }

SUFFIX="${SYMPHONY_SMOKE_SUFFIX:-20260716}"
IMPLEMENTER="${SYMPHONY_SMOKE_IMPLEMENTER:-codex}"
REVIEWER="${SYMPHONY_SMOKE_REVIEWER:-claude}"
TIMEOUT_SECONDS="${SYMPHONY_SMOKE_TIMEOUT_SECONDS:-10800}"
OWNER="$(gho api user --jq .login)"
PRIMARY="${OWNER}/openhands-symphony-smoke-${SUFFIX}"
PEER="${OWNER}/openhands-symphony-smoke-peer-${SUFFIX}"
PREFIX="[symphony smoke ${SUFFIX}]"

case "${IMPLEMENTER}" in claude|codex|antigravity) ;; *) echo "invalid implementer" >&2; exit 2 ;; esac
case "${REVIEWER}" in claude|codex|antigravity) ;; *) echo "invalid reviewer" >&2; exit 2 ;; esac
if [[ "${IMPLEMENTER}" == "${REVIEWER}" ]]; then
  echo "Smoke review must use a provider different from the implementer" >&2
  exit 2
fi

gho auth status --hostname github.com >/dev/null
gho repo view "${PRIMARY}" --json isPrivate --jq .isPrivate | grep -qx true
gho repo view "${PEER}" --json isPrivate --jq .isPrivate | grep -qx true
agentctl_o doctor
agentctl_o labels

ensure_issue() {
  local repository="$1"
  local title="$2"
  local body="$3"
  local existing
  existing="$(gho issue list --repo "${repository}" --state all --limit 100 --json number,title | \
    jq -r --arg title "${title}" 'map(select(.title == $title)) | first | .number // empty')"
  if [[ -n "${existing}" ]]; then
    echo "${existing}"
    return
  fi
  local url
  url="$(gho issue create --repo "${repository}" --title "${title}" --body "${body}")"
  echo "${url##*/}"
}

route_issue() {
  local repository="$1"
  local number="$2"
  local provider="$3"
  shift 3
  local args=(issue edit "${number}" --repo "${repository}" --add-label agent:ready --add-label "agent:${provider}")
  local label
  for label in "$@"; do
    args+=(--add-label "${label}")
  done
  gho "${args[@]}" >/dev/null
}

has_label() {
  local repository="$1" number="$2" label="$3"
  gho issue view "${number}" --repo "${repository}" --json labels \
    --jq ".labels | any(.name == \"${label}\")"
}

wait_for_label() {
  local repository="$1" number="$2" expected="$3"
  local deadline=$((SECONDS + TIMEOUT_SECONDS))
  while ((SECONDS < deadline)); do
    if [[ "$(has_label "${repository}" "${number}" "${expected}")" == "true" ]]; then
      echo "${repository}#${number}: observed ${expected}"
      return
    fi
    if [[ "${expected}" != "agent:failed" && "$(has_label "${repository}" "${number}" agent:failed)" == "true" ]]; then
      echo "${repository}#${number}: failed while waiting for ${expected}" >&2
      gho issue view "${number}" --repo "${repository}" --comments >&2
      exit 1
    fi
    sleep 5
  done
  echo "timeout waiting for ${repository}#${number} label ${expected}" >&2
  exit 1
}

pr_json() {
  local repository="$1" number="$2"
  gho pr list --repo "${repository}" --state open --limit 100 \
    --json number,url,isDraft,headRefName,body,title | \
    jq -c --arg close "Closes #${number}" 'map(select(.body | contains($close)))'
}

assert_one_draft_pr() {
  local repository="$1" number="$2"
  local prs
  prs="$(pr_json "${repository}" "${number}")"
  [[ "$(jq length <<<"${prs}")" == "1" ]]
  [[ "$(jq -r '.[0].isDraft' <<<"${prs}")" == "true" ]]
  echo "${repository}#${number}: one draft PR $(jq -r '.[0].url' <<<"${prs}")"
}

assert_one_status_comment() {
  local repository="$1" number="$2"
  local count
  count="$(gho api --paginate "repos/${repository}/issues/${number}/comments?per_page=100" | \
    jq '[.[] | select(.body | contains("<!-- openhands-symphony-status -->"))] | length')"
  [[ "${count}" == "1" ]]
}

assert_one_durable_job() {
  local repository="$1" number="$2"
  local count
  count="$(orchestrator sqlite3 /var/lib/openhands-symphony/state.db \
    "SELECT count(*) FROM jobs WHERE repository='${repository}' AND issue_number=${number};")"
  [[ "${count}" == "1" ]]
}

assert_pr_contract() {
  local repository="$1" number="$2"
  local pr
  pr="$(pr_json "${repository}" "${number}" | jq -r '.[0].number')"
  local payload
  payload="$(gho pr view "${pr}" --repo "${repository}" --json body,labels,isDraft)"
  [[ "$(jq -r .isDraft <<<"${payload}")" == "true" ]]
  jq -e --arg close "Closes #${number}" '
    (.body | contains($close)) and
    (.body | contains("Implementation provider:")) and
    (.body | contains("Run ID:")) and
    (.body | contains("## Validation")) and
    (.body | contains("## Unresolved risks")) and
    (.labels | any(.name == "generated-by-agent"))
  ' <<<"${payload}" >/dev/null
}

assert_guidance_question() {
  local repository="$1" number="$2"
  local body
  body="$(gho api --paginate "repos/${repository}/issues/${number}/comments?per_page=100" | \
    jq -r '[.[] | select(.body | contains("<!-- openhands-symphony-status -->"))] | first | .body')"
  grep -q 'State | `needs-guidance`' <<<"${body}"
  if grep -q 'Action required / latest failure | —' <<<"${body}"; then
    echo "needs-guidance status omitted its focused question" >&2
    exit 1
  fi
}

SUCCESS="$(ensure_issue "${PRIMARY}" "${PREFIX} successful implementation" \
  $'Add a multiply(left, right) function to calculator.py. Add unittest coverage for positive, negative, and zero inputs. Update README.md with a short usage example. Run the repository test command. No external service or product decision is required.')"
REVIEW="$(ensure_issue "${PRIMARY}" "${PREFIX} independent review" \
  $'Add a subtract(left, right) function to calculator.py with unittest coverage for integers and negative results. Document it in README.md. Keep the change dependency-free and run all tests. An independent reviewer must assess correctness, test coverage, and residual risk.')"
GUIDANCE="$(ensure_issue "${PRIMARY}" "${PREFIX} needs guidance" \
  $'Add a round_currency(value) function, but the required rounding rule is intentionally unspecified. Do not choose between bankers rounding and half-up rounding. This is a required product decision: stop without editing code and ask exactly which rounding rule should be used.')"
CONCURRENCY_A="$(ensure_issue "${PRIMARY}" "${PREFIX} concurrency maximum" \
  $'Add maximum(values) without using max(). Raise ValueError for an empty iterable and add thorough unittest coverage. Run all tests.')"
CONCURRENCY_B="$(ensure_issue "${PRIMARY}" "${PREFIX} concurrency minimum" \
  $'Add minimum(values) without using min(). Raise ValueError for an empty iterable and add thorough unittest coverage. Run all tests.')"
PEER_ISSUE="$(ensure_issue "${PEER}" "${PREFIX} parallel repository" \
  $'Add clamp(value, lower, upper), reject an inverted range with ValueError, and add thorough unittest coverage. Run all tests.')"
RECOVERY="$(ensure_issue "${PRIMARY}" "${PREFIX} expired lease recovery" \
  $'Add arithmetic_mean(values), reject an empty iterable with ValueError, remain dependency-free, and add unittest coverage. Run all tests.')"

route_issue "${PRIMARY}" "${SUCCESS}" "${IMPLEMENTER}"
orchestrator python3 "${ROOT_DIR}/scripts/send_duplicate_webhook.py" "${PRIMARY}#${SUCCESS}"
route_issue "${PRIMARY}" "${REVIEW}" "${IMPLEMENTER}" review:required "review:${REVIEWER}"
route_issue "${PRIMARY}" "${GUIDANCE}" "${IMPLEMENTER}"
agentctl_o reconcile >/dev/null

wait_for_label "${PRIMARY}" "${SUCCESS}" agent:pr-open
wait_for_label "${PRIMARY}" "${REVIEW}" agent:pr-open
wait_for_label "${PRIMARY}" "${GUIDANCE}" agent:needs-guidance
assert_one_draft_pr "${PRIMARY}" "${SUCCESS}"
assert_one_draft_pr "${PRIMARY}" "${REVIEW}"
[[ "$(jq length <<<"$(pr_json "${PRIMARY}" "${GUIDANCE}")")" == "0" ]]
assert_one_status_comment "${PRIMARY}" "${SUCCESS}"
assert_one_status_comment "${PRIMARY}" "${REVIEW}"
assert_one_status_comment "${PRIMARY}" "${GUIDANCE}"
assert_one_durable_job "${PRIMARY}" "${SUCCESS}"
assert_pr_contract "${PRIMARY}" "${SUCCESS}"
assert_pr_contract "${PRIMARY}" "${REVIEW}"
assert_guidance_question "${PRIMARY}" "${GUIDANCE}"

REVIEW_PR="$(pr_json "${PRIMARY}" "${REVIEW}" | jq -r '.[0].number')"
REVIEWS="$(gho api "repos/${PRIMARY}/pulls/${REVIEW_PR}/reviews")"
REVIEW_COUNT="$(jq length <<<"${REVIEWS}")"
[[ "${REVIEW_COUNT}" -ge 1 ]]
jq -e 'any(.body as $body |
  ($body | contains("## Blocker")) and
  ($body | contains("## High")) and
  ($body | contains("## Medium")) and
  ($body | contains("## Low")) and
  ($body | contains("## Validation")) and
  ($body | contains("## Residual risks")))' <<<"${REVIEWS}" >/dev/null
REVIEW_IDS="$(orchestrator sqlite3 -separator '|' /var/lib/openhands-symphony/state.db \
  "SELECT conversation_id,review_conversation_id FROM jobs WHERE repository='${PRIMARY}' AND issue_number=${REVIEW};")"
IMPLEMENTATION_CONVERSATION="${REVIEW_IDS%%|*}"
REVIEW_CONVERSATION="${REVIEW_IDS#*|}"
[[ -n "${IMPLEMENTATION_CONVERSATION}" && -n "${REVIEW_CONVERSATION}" ]]
[[ "${IMPLEMENTATION_CONVERSATION}" != "${REVIEW_CONVERSATION}" ]]

route_issue "${PRIMARY}" "${CONCURRENCY_A}" "${IMPLEMENTER}"
route_issue "${PRIMARY}" "${CONCURRENCY_B}" "${IMPLEMENTER}"
route_issue "${PEER}" "${PEER_ISSUE}" "${REVIEWER}"
agentctl_o reconcile >/dev/null

OBSERVED_CONCURRENCY=false
OBSERVED_PEER_OVERLAP=false
deadline=$((SECONDS + 120))
while ((SECONDS < deadline)); do
  a_running="$(has_label "${PRIMARY}" "${CONCURRENCY_A}" agent:running)"
  b_running="$(has_label "${PRIMARY}" "${CONCURRENCY_B}" agent:running)"
  a_queued="$(has_label "${PRIMARY}" "${CONCURRENCY_A}" agent:queued)"
  b_queued="$(has_label "${PRIMARY}" "${CONCURRENCY_B}" agent:queued)"
  peer_running="$(has_label "${PEER}" "${PEER_ISSUE}" agent:running)"
  peer_open="$(has_label "${PEER}" "${PEER_ISSUE}" agent:pr-open)"
  if [[ ("${a_running}" == "true" && "${b_queued}" == "true") || \
        ("${b_running}" == "true" && "${a_queued}" == "true") ]]; then
    OBSERVED_CONCURRENCY=true
    if [[ "${peer_running}" == "true" || "${peer_open}" == "true" ]]; then
      OBSERVED_PEER_OVERLAP=true
      echo "same-repository serialization and cross-repository progress are visible"
      break
    fi
  fi
  sleep 1
done
[[ "${OBSERVED_CONCURRENCY}" == "true" ]]
[[ "${OBSERVED_PEER_OVERLAP}" == "true" ]]

wait_for_label "${PRIMARY}" "${CONCURRENCY_A}" agent:pr-open
wait_for_label "${PRIMARY}" "${CONCURRENCY_B}" agent:pr-open
wait_for_label "${PEER}" "${PEER_ISSUE}" agent:pr-open
assert_one_draft_pr "${PRIMARY}" "${CONCURRENCY_A}"
assert_one_draft_pr "${PRIMARY}" "${CONCURRENCY_B}"
assert_one_draft_pr "${PEER}" "${PEER_ISSUE}"
assert_pr_contract "${PRIMARY}" "${CONCURRENCY_A}"
assert_pr_contract "${PRIMARY}" "${CONCURRENCY_B}"
assert_pr_contract "${PEER}" "${PEER_ISSUE}"

route_issue "${PRIMARY}" "${RECOVERY}" "${IMPLEMENTER}"
agentctl_o reconcile >/dev/null
wait_for_label "${PRIMARY}" "${RECOVERY}" agent:running
sudo systemctl stop openhands-symphony.target
orchestrator python3 "${ROOT_DIR}/scripts/expire_smoke_lease.py" "${PRIMARY}#${RECOVERY}"
sudo systemctl start openhands-symphony.target
agentctl_o reconcile >/dev/null

deadline=$((SECONDS + TIMEOUT_SECONDS))
RECOVERY_TERMINAL=false
while ((SECONDS < deadline)); do
  if [[ "$(has_label "${PRIMARY}" "${RECOVERY}" agent:pr-open)" == "true" ]]; then
    assert_one_draft_pr "${PRIMARY}" "${RECOVERY}"
    assert_pr_contract "${PRIMARY}" "${RECOVERY}"
    RECOVERY_TERMINAL=true
    break
  fi
  if [[ "$(has_label "${PRIMARY}" "${RECOVERY}" agent:failed)" == "true" ]]; then
    echo "${PRIMARY}#${RECOVERY}: safely reached terminal failure after recovery simulation"
    RECOVERY_TERMINAL=true
    break
  fi
  sleep 5
done
[[ "${RECOVERY_TERMINAL}" == "true" ]]
assert_one_status_comment "${PRIMARY}" "${RECOVERY}"
assert_one_durable_job "${PRIMARY}" "${RECOVERY}"

REPORT="/var/lib/openhands-symphony/reports/smoke-${SUFFIX}.json"
TMP_REPORT="$(mktemp)"
jq -n \
  --arg primary "${PRIMARY}" --arg peer "${PEER}" \
  --argjson success "${SUCCESS}" --argjson review "${REVIEW}" --argjson guidance "${GUIDANCE}" \
  --argjson concurrency_a "${CONCURRENCY_A}" --argjson concurrency_b "${CONCURRENCY_B}" \
  --argjson peer_issue "${PEER_ISSUE}" --argjson recovery "${RECOVERY}" \
  --argjson review_count "${REVIEW_COUNT}" \
  '{primary_repository:$primary,peer_repository:$peer,issues:{success:$success,review:$review,guidance:$guidance,concurrency_a:$concurrency_a,concurrency_b:$concurrency_b,peer:$peer_issue,recovery:$recovery},github_review_count:$review_count,production_auto_merge:false}' \
  > "${TMP_REPORT}"
sudo install -o openhands-symphony -g openhands-symphony -m 0600 "${TMP_REPORT}" "${REPORT}"
rm -f "${TMP_REPORT}"
echo "Smoke test complete; artifact inventory: ${REPORT}"
