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
if [[ -z "${command//[$' \t\r\n']/}" ]]; then
  echo "error: $env_name must contain a command." >&2
  exit 2
fi

if [[ "$append_args" == true ]]; then
  # Trim trailing whitespace so commands from YAML block scalars (which end
  # with a newline) keep the appended arguments on the same command line.
  while [[ "$command" == *$'\n' || "$command" == *$'\r' || "$command" == *' ' || "$command" == *$'\t' ]]; do
    command="${command%?}"
  done
  last_line="${command##*$'\n'}"
  if [[ "$last_line" == '#'* || "$last_line" == *' #'* || "$last_line" == *$'\t#'* ]]; then
    echo "error: $env_name ends with a '#' comment, which would swallow the appended arguments." >&2
    exit 2
  fi
fi

tmp_dir="${RUNNER_TEMP:-/tmp}"
# Keep the XXXXXX template at the end: BSD mktemp does not substitute
# non-trailing X's and would create a fixed, predictable filename.
command_file="$(mktemp "$tmp_dir/release-command.XXXXXX")"
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
