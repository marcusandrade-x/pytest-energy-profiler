#!/usr/bin/env bash
# Launcher protocol for phase1_runner.py: sudo_unshare.sh -- COMMAND ARGS...
set -euo pipefail

if [[ "${1:-}" != "--" || "$#" -lt 2 ]]; then
  echo "usage: sudo_unshare.sh -- COMMAND [ARGS...]" >&2
  exit 64
fi
shift

uid="${PHASE1_RUN_UID:?PHASE1_RUN_UID is required}"
gid="${PHASE1_RUN_GID:?PHASE1_RUN_GID is required}"
if [[ "$uid" == 0 || "$uid" != "$(/usr/bin/id -u)" || "$gid" != "$(/usr/bin/id -g)" ]]; then
  echo "refusing identity mismatch or UID 0" >&2
  exit 65
fi
docker_gid="$(/usr/bin/getent group docker | /usr/bin/cut -d: -f3 || true)"
if [[ -n "$docker_gid" && "$gid" == "$docker_gid" ]]; then
  echo "refusing docker as primary group" >&2
  exit 65
fi

safe_names=(PATH HOME TMPDIR LANG LC_ALL PYTHONPATH PYTHONUNBUFFERED
            PHASE1_PROJECT_ID PHASE1_PROJECT_ROOT PHASE1_RUN_ID PHASE1_OUTPUT
            PHASE1_STATIC_INVENTORY PHASE1_PRIOR_DEPENDENCY_MAP PHASE1_CIRCUIT_BREAKER_K)
env_args=()
for name in "${safe_names[@]}"; do
  if [[ -v "$name" ]]; then env_args+=("$name=${!name}"); fi
done

exec /usr/bin/sudo -n /usr/local/sbin/phase1-netns-root \
  "$uid" "$gid" -- "${env_args[@]}" -- "$@"
