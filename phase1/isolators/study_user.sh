#!/usr/bin/env bash
# Network-enabled preparation launcher. Use only for pip/venv installation;
# package build hooks run as the dedicated user with a minimal environment.
set -euo pipefail

if [[ "${1:-}" != "--" || "$#" -lt 2 ]]; then
  echo "usage: study_user.sh -- COMMAND [ARGS...]" >&2
  exit 64
fi
shift
uid="${PHASE1_RUN_UID:?PHASE1_RUN_UID is required}"
gid="${PHASE1_RUN_GID:?PHASE1_RUN_GID is required}"
if [[ "$uid" == 0 ]]; then
  echo "refusing UID 0" >&2
  exit 65
fi
docker_gid="$(getent group docker | cut -d: -f3 || true)"
if [[ -n "$docker_gid" && "$gid" == "$docker_gid" ]]; then
  echo "refusing docker as primary group" >&2
  exit 65
fi

safe_names=(PATH LANG LC_ALL PIP_DISABLE_PIP_VERSION_CHECK
            PHASE1_RUN_UID PHASE1_RUN_GID)
env_args=("HOME=${PHASE1_HOME:?PHASE1_HOME is required}" \
          "TMPDIR=${PHASE1_TMPDIR:?PHASE1_TMPDIR is required}")
for name in "${safe_names[@]}"; do
  if [[ -v "$name" ]]; then env_args+=("$name=${!name}"); fi
done

exec /usr/bin/sudo -n /usr/bin/setpriv --reuid="$uid" --regid="$gid" \
  --clear-groups -- /usr/bin/env -i "${env_args[@]}" "$@"
