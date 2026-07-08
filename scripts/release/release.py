#!/usr/bin/env python3
"""Release workflow helpers for Roc package releases."""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class ReleaseError(Exception):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser("validate-version")
    validate.add_argument("version")
    validate.set_defaults(func=cmd_validate_version)

    available = subcommands.add_parser("check-availability")
    available.add_argument("version")
    available.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    available.set_defaults(func=cmd_check_availability)

    previous = subcommands.add_parser("resolve-previous-url")
    previous.add_argument("--provided-url", default="")
    previous.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    previous.add_argument("--output-file", required=True)
    previous.set_defaults(func=cmd_resolve_previous_url)

    bump = subcommands.add_parser("run-bump-check")
    bump.add_argument("--mode", required=True, choices=["warn", "require", "off"])
    bump.add_argument("--version", required=True)
    bump.add_argument("--entrypoint", required=True)
    bump.add_argument("--previous-url", default="")
    bump.add_argument("--output-file", required=True)
    bump.set_defaults(func=cmd_run_bump_check)

    bundles = subcommands.add_parser("prepare-bundles")
    bundles.add_argument("--bundle-glob", required=True)
    bundles.add_argument("--bundle-manifest-path", default="")
    bundles.add_argument("--test-os-json", required=True)
    bundles.add_argument("--workspace", default=".")
    bundles.add_argument("--bundle-dir", required=True)
    bundles.add_argument("--matrix-file", required=True)
    bundles.add_argument("--release-list-file", required=True)
    bundles.add_argument("--github-output", default="")
    bundles.set_defaults(func=cmd_prepare_bundles)

    compact = subcommands.add_parser("compact-json")
    compact.add_argument("path")
    compact.set_defaults(func=cmd_compact_json)

    notes = subcommands.add_parser("make-release-notes")
    notes.add_argument("--repo", required=True)
    notes.add_argument("--version", required=True)
    notes.add_argument("--target", required=True)
    notes.add_argument("--bump-output", required=True)
    notes.add_argument("--output-file", required=True)
    notes.set_defaults(func=cmd_make_release_notes)

    publish = subcommands.add_parser("publish-release")
    publish.add_argument("--version", required=True)
    publish.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    publish.add_argument("--target", default=os.environ.get("GITHUB_SHA", ""))
    publish.add_argument("--notes-file", required=True)
    publish.add_argument("--bundle-dir", required=True)
    publish.add_argument("--skip-availability-check", action="store_true")
    publish.set_defaults(func=cmd_publish_release)

    snapshot_docs = subcommands.add_parser("snapshot-docs")
    snapshot_docs.add_argument("--docs-root", required=True)
    snapshot_docs.add_argument("--snapshot-file", required=True)
    snapshot_docs.set_defaults(func=cmd_snapshot_docs)

    docs_index = subcommands.add_parser("write-docs-index")
    docs_index.add_argument("--docs-root", required=True)
    docs_index.add_argument("--docs-version", required=True)
    docs_index.add_argument("--repo-name", required=True)
    docs_index.set_defaults(func=cmd_write_docs_index)

    validate_docs = subcommands.add_parser("validate-docs")
    validate_docs.add_argument("--docs-root", required=True)
    validate_docs.add_argument("--docs-version", required=True)
    validate_docs.add_argument("--snapshot-file", required=True)
    validate_docs.set_defaults(func=cmd_validate_docs)

    args = parser.parse_args()
    try:
        return args.func(args)
    except ReleaseError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


def cmd_validate_version(args: argparse.Namespace) -> int:
    validate_release_version(args.version)
    return 0


def cmd_check_availability(args: argparse.Namespace) -> int:
    version = validate_release_version(args.version)
    repo = require_repo(args.repo)

    local = run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{version}"])
    if local.returncode == 0:
        raise ReleaseError(f"git tag {version!r} already exists locally")

    remote = run(["git", "ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{version}"])
    if remote.returncode == 0:
        raise ReleaseError(f"git tag {version!r} already exists on origin")
    if remote.returncode not in (2,):
        raise ReleaseError(f"could not check remote tag {version!r}: {trim_output(remote)}")

    release = run(["gh", "api", f"repos/{repo}/releases/tags/{version}"])
    if release.returncode == 0:
        raise ReleaseError(f"GitHub release {version!r} already exists")
    if not is_github_not_found(release):
        raise ReleaseError(f"could not check GitHub release {version!r}: {trim_output(release)}")

    return 0


def cmd_resolve_previous_url(args: argparse.Namespace) -> int:
    if args.provided_url:
        write_text(args.output_file, args.provided_url + "\n")
        return 0

    repo = require_repo(args.repo)
    latest = run(["gh", "api", f"repos/{repo}/releases/latest"])
    if latest.returncode != 0:
        if is_github_not_found(latest):
            write_text(args.output_file, "\n")
            return 0
        raise ReleaseError(f"could not look up latest release: {trim_output(latest)}")

    try:
        data = json.loads(latest.stdout)
    except json.JSONDecodeError as err:
        raise ReleaseError(f"latest release response was not valid JSON: {err}") from err

    assets = data.get("assets", [])
    matches = [
        asset.get("browser_download_url", "")
        for asset in assets
        if str(asset.get("name", "")).endswith(".tar.zst")
    ]
    matches = [url for url in matches if url]
    if len(matches) > 1:
        raise ReleaseError(
            "latest release has multiple .tar.zst assets; set previous_release_url explicitly"
        )

    write_text(args.output_file, (matches[0] if matches else "") + "\n")
    return 0


def cmd_run_bump_check(args: argparse.Namespace) -> int:
    version = validate_release_version(args.version)
    mode = args.mode

    if mode == "off":
        write_text(args.output_file, "roc bump check skipped because bump_check is off.\n")
        return 0

    if not args.previous_url:
        write_text(args.output_file, "No previous release bundle found; roc bump check skipped.\n")
        return 0

    result = run(
        [
            "roc",
            "bump",
            "--old",
            args.previous_url,
            "--expect",
            version,
            args.entrypoint,
        ]
    )
    output = trim_output(result)
    if not output:
        output = "roc bump completed without output."
    write_text(args.output_file, output.rstrip() + "\n")

    if result.returncode == 0:
        return 0

    message = f"roc bump failed for expected version {version}."
    if mode == "warn":
        print(f"warning: {message}", file=sys.stderr)
        print(output, file=sys.stderr)
        return 0

    print(output, file=sys.stderr)
    raise ReleaseError(message)


def cmd_prepare_bundles(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    bundle_dir = Path(args.bundle_dir)
    matrix_file = Path(args.matrix_file)
    release_list_file = Path(args.release_list_file)

    glob_paths = find_bundle_glob(args.bundle_glob, workspace)
    if not glob_paths:
        raise ReleaseError(f"bundle_glob matched no files: {args.bundle_glob}")

    if args.bundle_manifest_path:
        bundles = bundles_from_manifest(args.bundle_manifest_path, workspace)
        manifest_paths = {bundle["path"] for bundle in bundles}
        glob_path_set = set(glob_paths)
        missing_manifest = sorted(glob_path_set - manifest_paths)
        extra_manifest = sorted(manifest_paths - glob_path_set)
        if missing_manifest:
            raise ReleaseError(
                "bundle_glob matched files without manifest test entries: "
                + ", ".join(str(path.relative_to(workspace)) for path in missing_manifest)
            )
        if extra_manifest:
            raise ReleaseError(
                "bundle manifest references files not matched by bundle_glob: "
                + ", ".join(str(path.relative_to(workspace)) for path in extra_manifest)
            )
    else:
        test_os = parse_test_os_json(args.test_os_json)
        bundles = default_bundles(glob_paths, test_os)

    artifact_names: set[str] = set()
    matrix: list[dict[str, str]] = []
    release_list: list[dict[str, str]] = []

    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    for bundle in bundles:
        source = bundle["path"]
        artifact_file = source.name
        if artifact_file in artifact_names:
            raise ReleaseError(f"duplicate release asset filename: {artifact_file}")
        artifact_names.add(artifact_file)
        shutil.copy2(source, bundle_dir / artifact_file)

        release_list.append(
            {
                "name": bundle["name"],
                "artifact_file": artifact_file,
                "source_path": str(source.relative_to(workspace)),
            }
        )
        for runner in bundle["test_os"]:
            matrix.append(
                {
                    "bundle_name": bundle["name"],
                    "artifact_file": artifact_file,
                    "os": runner,
                }
            )

    if not matrix:
        raise ReleaseError("no bundle test matrix entries were produced")

    write_json(matrix_file, matrix)
    write_json(release_list_file, release_list)

    if args.github_output:
        append_github_output(args.github_output, "test_matrix", compact_json(matrix))
        append_github_output(args.github_output, "release_bundles", compact_json(release_list))

    return 0


def cmd_compact_json(args: argparse.Namespace) -> int:
    with open(args.path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    print(compact_json(data))
    return 0


def cmd_make_release_notes(args: argparse.Namespace) -> int:
    version = validate_release_version(args.version)
    result = run(
        [
            "gh",
            "api",
            f"repos/{args.repo}/releases/generate-notes",
            "--method",
            "POST",
            "-f",
            f"tag_name={version}",
            "-f",
            f"target_commitish={args.target}",
        ]
    )
    if result.returncode != 0:
        raise ReleaseError(f"could not generate GitHub release notes: {trim_output(result)}")

    try:
        generated = json.loads(result.stdout)
    except json.JSONDecodeError as err:
        raise ReleaseError(f"release notes response was not valid JSON: {err}") from err

    body = str(generated.get("body", "")).strip()
    if not body:
        body = f"Release {version}"

    bump_output = Path(args.bump_output)
    if bump_output.is_file():
        bump_text = bump_output.read_text(encoding="utf-8").strip()
        if is_meaningful_bump_output(bump_text):
            body += "\n\n## Roc API Changes\n\n```text\n" + bump_text + "\n```"

    write_text(args.output_file, body.rstrip() + "\n")
    return 0


def cmd_publish_release(args: argparse.Namespace) -> int:
    version = validate_release_version(args.version)
    repo = require_repo(args.repo)
    target = require_target(args.target)
    notes_file = require_nonempty_file(args.notes_file, "release notes file")
    assets = release_assets(args.bundle_dir)

    if not args.skip_availability_check:
        cmd_check_availability(namespace(version=version, repo=repo))

    run_required(
        ["git", "config", "user.name", "github-actions[bot]"],
        "could not configure git user.name",
    )
    run_required(
        [
            "git",
            "config",
            "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ],
        "could not configure git user.email",
    )
    run_required(["git", "tag", version, target], f"could not create git tag {version!r}")
    run_required(
        ["git", "push", "origin", f"refs/tags/{version}"],
        f"could not push git tag {version!r}",
    )
    run_required(
        [
            "gh",
            "release",
            "create",
            version,
            *[str(asset) for asset in assets],
            "--repo",
            repo,
            "--target",
            target,
            "--title",
            version,
            "--notes-file",
            str(notes_file),
        ],
        f"could not create GitHub release {version!r}",
    )
    return 0


def cmd_snapshot_docs(args: argparse.Namespace) -> int:
    docs_root = Path(args.docs_root)
    versions = sorted(version_dirs(docs_root))
    write_json(args.snapshot_file, versions)
    return 0


def cmd_write_docs_index(args: argparse.Namespace) -> int:
    docs_version = validate_release_version(args.docs_version)
    docs_root = Path(args.docs_root)
    repo_name = clean_repo_name(args.repo_name)
    target = f"/{repo_name}/{docs_version}/"
    escaped_target = html.escape(target, quote=True)
    content = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        f'  <meta http-equiv="refresh" content="0; url={escaped_target}">\n'
        f'  <link rel="canonical" href="{escaped_target}">\n'
        f"  <title>Redirecting to {html.escape(docs_version)}</title>\n"
        "</head>\n"
        "<body>\n"
        f'  <p><a href="{escaped_target}">Redirecting to {html.escape(docs_version)}</a></p>\n'
        "</body>\n"
        "</html>\n"
    )
    docs_root.mkdir(parents=True, exist_ok=True)
    write_text(docs_root / "index.html", content)
    return 0


def cmd_validate_docs(args: argparse.Namespace) -> int:
    docs_version = validate_release_version(args.docs_version)
    docs_root = Path(args.docs_root)
    if not docs_root.is_dir():
        raise ReleaseError(f"docs_root does not exist: {docs_root}")
    if not any(docs_root.iterdir()):
        raise ReleaseError(f"docs_root is empty: {docs_root}")

    version_root = docs_root / docs_version
    if not version_root.is_dir():
        raise ReleaseError(f"generated docs version directory is missing: {version_root}")
    if not (version_root / "index.html").is_file():
        raise ReleaseError(f"generated docs version index is missing: {version_root / 'index.html'}")

    before = set(read_json(args.snapshot_file))
    after = set(version_dirs(docs_root))
    missing = sorted(version for version in before if version != docs_version and version not in after)
    if missing:
        raise ReleaseError(
            "docs generation removed historical version directories: " + ", ".join(missing)
        )

    return 0


def validate_release_version(version: str) -> str:
    if not version:
        raise ReleaseError("release version is required")
    match = SEMVER_RE.match(version)
    if not match:
        raise ReleaseError(
            f"release version must be strict X.Y.Z semver without a leading v: {version!r}"
        )
    parts = tuple(int(part) for part in match.groups())
    if parts == (0, 0, 0):
        raise ReleaseError("release version 0.0.0 is reserved and cannot be published")
    return version


def parse_test_os_json(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as err:
        raise ReleaseError(f"test_os_json is not valid JSON: {err}") from err
    if not isinstance(parsed, list) or not parsed:
        raise ReleaseError("test_os_json must be a non-empty JSON array")
    runners: list[str] = []
    for item in parsed:
        if not isinstance(item, str) or not item:
            raise ReleaseError("test_os_json entries must be non-empty strings")
        runners.append(item)
    return runners


def find_bundle_glob(pattern: str, workspace: Path) -> list[Path]:
    if "\n" in pattern or "\r" in pattern:
        raise ReleaseError("bundle_glob must not contain newlines")
    if Path(pattern).is_absolute():
        raise ReleaseError("bundle_glob must be relative to the workspace")
    matches = glob.glob(str(workspace / pattern), recursive=True)
    paths = [safe_existing_file(path, workspace) for path in matches]
    return sorted(set(paths))


def bundles_from_manifest(manifest_path: str, workspace: Path) -> list[dict[str, Any]]:
    manifest_file = safe_existing_file(manifest_path, workspace)
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ReleaseError(f"bundle manifest is not valid JSON: {err}") from err

    if not isinstance(data, list) or not data:
        raise ReleaseError("bundle manifest must be a non-empty JSON array")

    names: set[str] = set()
    bundles: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ReleaseError(f"bundle manifest entry {index} must be an object")
        name = item.get("name")
        path = item.get("path")
        test_os = item.get("test_os")
        if not isinstance(name, str) or not name:
            raise ReleaseError(f"bundle manifest entry {index} has invalid name")
        if "\n" in name or "\r" in name:
            raise ReleaseError(f"bundle manifest entry {name!r} contains a newline")
        if name in names:
            raise ReleaseError(f"duplicate bundle name in manifest: {name}")
        names.add(name)
        if not isinstance(path, str) or not path:
            raise ReleaseError(f"bundle manifest entry {name!r} has invalid path")
        if not isinstance(test_os, list) or not test_os:
            raise ReleaseError(f"bundle manifest entry {name!r} must have non-empty test_os")
        runners: list[str] = []
        for runner in test_os:
            if not isinstance(runner, str) or not runner:
                raise ReleaseError(f"bundle manifest entry {name!r} has invalid test_os runner")
            runners.append(runner)
        bundles.append(
            {
                "name": name,
                "path": safe_existing_file(path, workspace),
                "test_os": runners,
            }
        )

    return bundles


def default_bundles(paths: list[Path], test_os: list[str]) -> list[dict[str, Any]]:
    bundles: list[dict[str, Any]] = []
    for path in paths:
        name = "default" if len(paths) == 1 else path.name.removesuffix(".tar.zst")
        bundles.append({"name": name, "path": path, "test_os": test_os})
    return bundles


def safe_existing_file(path_text: str | Path, workspace: Path) -> Path:
    text = str(path_text)
    if "\n" in text or "\r" in text:
        raise ReleaseError(f"path contains a newline: {text!r}")
    path = Path(text)
    candidate = path if path.is_absolute() else workspace / path
    resolved = candidate.resolve()
    workspace_resolved = workspace.resolve()
    try:
        resolved.relative_to(workspace_resolved)
    except ValueError as err:
        raise ReleaseError(f"path escapes the workspace: {text}") from err
    if not resolved.is_file():
        raise ReleaseError(f"file does not exist: {text}")
    return resolved


def version_dirs(docs_root: Path) -> list[str]:
    if not docs_root.is_dir():
        return []
    return [
        child.name
        for child in docs_root.iterdir()
        if child.is_dir() and SEMVER_RE.match(child.name) and child.name != "0.0.0"
    ]


def clean_repo_name(repo_name: str) -> str:
    if "/" in repo_name:
        repo_name = repo_name.rsplit("/", 1)[1]
    if not repo_name or "\n" in repo_name or "\r" in repo_name or "/" in repo_name:
        raise ReleaseError(f"invalid repository name for docs redirect: {repo_name!r}")
    return repo_name


def is_meaningful_bump_output(text: str) -> bool:
    if not text:
        return False
    skipped_prefixes = (
        "roc bump check skipped",
        "No previous release bundle found;",
    )
    return not any(text.startswith(prefix) for prefix in skipped_prefixes)


def require_repo(repo: str) -> str:
    if not repo or "/" not in repo:
        raise ReleaseError("GITHUB_REPOSITORY or --repo must be set to owner/name")
    return repo


def require_target(target: str) -> str:
    if not target or "\n" in target or "\r" in target:
        raise ReleaseError("GITHUB_SHA or --target must be set to a commit-ish")
    return target


def require_nonempty_file(path_text: str | Path, description: str) -> Path:
    path = Path(path_text)
    if not path.is_file():
        raise ReleaseError(f"{description} is missing: {path}")
    if path.stat().st_size == 0:
        raise ReleaseError(f"{description} is empty: {path}")
    return path


def release_assets(bundle_dir_text: str | Path) -> list[Path]:
    bundle_dir = Path(bundle_dir_text)
    if not bundle_dir.is_dir():
        raise ReleaseError(f"release bundle directory is missing: {bundle_dir}")
    assets = sorted(path for path in bundle_dir.iterdir() if path.is_file())
    if not assets:
        raise ReleaseError(f"release bundle directory has no files: {bundle_dir}")
    for asset in assets:
        if "\n" in asset.name or "\r" in asset.name:
            raise ReleaseError(f"release asset filename contains a newline: {asset.name!r}")
    return assets


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_required(command: list[str], message: str) -> None:
    result = run(command)
    if result.returncode != 0:
        raise ReleaseError(f"{message}: {trim_output(result)}")


def namespace(**kwargs: Any) -> object:
    return type("Args", (), kwargs)()


def is_github_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    text = f"{result.stdout}\n{result.stderr}"
    return "HTTP 404" in text or "Not Found" in text


def trim_output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout + result.stderr).strip()


def write_text(path: str | Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def write_json(path: str | Path, data: Any) -> None:
    write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_json(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def compact_json(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def append_github_output(path: str | Path, name: str, value: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


if __name__ == "__main__":
    sys.exit(main())
