#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: run-command.sh [--append-args] ENV_VAR_NAME [args...]" >&2
}

append_args=false
if [[ "${1:-}" == "--append-args" ]]; then
  append_args=true
  shift
fi

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

env_name="$1"
shift

command="${!env_name:-}"
if [[ -z "$command" ]]; then
  echo "Skipping empty command hook: $env_name"
  exit 0
fi

tmp_dir="${RUNNER_TEMP:-/tmp}"
command_file="$(mktemp "$tmp_dir/release-command.XXXXXX.sh")"
cleanup() {
  rm -f "$command_file"
}
trap cleanup EXIT

{
  echo "set -euo pipefail"
  if [[ "$append_args" == true ]]; then
    printf "%s" "$command"
    for arg in "$@"; do
      printf " %q" "$arg"
    done
    printf "\n"
  else
    printf "%s\n" "$command"
  fi
} > "$command_file"

bash "$command_file"
