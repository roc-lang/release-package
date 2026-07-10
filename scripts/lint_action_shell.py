#!/usr/bin/env python3
"""Run shellcheck over the run: blocks of every composite action.

actionlint only analyzes workflow files, so the bash embedded in
actions/*/action.yml gets no lint coverage without this script.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if shutil.which("shellcheck") is None:
        print("error: shellcheck is not installed", file=sys.stderr)
        return 2

    action_files = sorted((ROOT / "actions").glob("*/action.yml"))
    if not action_files:
        print("error: no actions/*/action.yml files found", file=sys.stderr)
        return 2

    failures = 0
    for action_file in action_files:
        data = yaml.safe_load(action_file.read_text(encoding="utf-8"))
        steps = (data.get("runs") or {}).get("steps") or []
        for index, step in enumerate(steps):
            script = step.get("run")
            if not script:
                continue
            label = f"{action_file.relative_to(ROOT)} step {step.get('id', index)}"
            if not check_script(script, sorted((step.get("env") or {}).keys()), label):
                failures += 1

    if failures:
        print(f"error: shellcheck failed for {failures} action step(s)", file=sys.stderr)
        return 1
    print(f"shellcheck passed for {len(action_files)} composite action(s)")
    return 0


def check_script(script: str, env_names: list[str], label: str) -> bool:
    # Pre-declare the step's env: variables so shellcheck does not report
    # them as unassigned (SC2154).
    declarations = "".join(f': "${{{name}:=}}"\n' for name in env_names)
    source = "#!/usr/bin/env bash\n" + declarations + script
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8") as handle:
        handle.write(source)
        script_path = handle.name
    try:
        result = subprocess.run(
            ["shellcheck", "--shell=bash", "--severity=info", script_path],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    finally:
        Path(script_path).unlink(missing_ok=True)
    if result.returncode != 0:
        print(f"--- {label}")
        print(result.stdout)
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
