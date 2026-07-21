#!/usr/bin/env bash

# Shared separately from install.sh so platform policy can be regression-tested
# without running the privileged installer.
symphony_require_supported_platform() {
  local distribution_id="${1:-}"
  local distribution_version="${2:-}"
  local pretty_name="${3:-unknown}"

  if [[ "${distribution_id}" != "ubuntu" ]]; then
    echo "Unsupported distribution: Ubuntu 24.04 or 26.04 LTS is required; found ${pretty_name}" >&2
    return 1
  fi

  case "${distribution_version}" in
    24.04|26.04) return 0 ;;
    *)
      echo "Unsupported distribution: Ubuntu 24.04 or 26.04 LTS is required; found ${pretty_name}" >&2
      return 1
      ;;
  esac
}
