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
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RELEASE_VERSION_RE = re.compile(
    r"(?P<base>"
    r"(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r")(?:-(?P<prerelease>"
    r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*"
    r"))?"
)

BUNDLE_SUFFIX = ".tar.zst"

# Marker file that lets prepare-bundles recognize a bundle_dir it created, so
# it never deletes a directory holding files it does not own.
BUNDLE_DIR_MARKER = ".created-by-prepare-bundles"

# make-release-notes decides whether a bump output file holds real roc bump
# output by these prefixes; the writers below and is_meaningful_bump_output
# must stay in sync through these constants.
BUMP_SKIP_PREFIX = "roc bump check skipped"
BUMP_NO_PREVIOUS_PREFIX = "No previous release bundle found;"


@dataclass(frozen=True)
class ReleaseVersion:
    full: str
    base: str
    prerelease: str

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)


class ReleaseError(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser("validate-release")
    validate.add_argument("--release-version", default="")
    validate.add_argument("--dry-run", choices=["true", "false"], default="false")
    validate.add_argument("--dry-run-version", default="999.999.999")
    validate.add_argument("--docs-version", default="")
    validate.add_argument("--check-availability", choices=["true", "false"], default="true")
    validate.add_argument("--repo", default="")
    validate.add_argument("--github-output", default="")
    validate.add_argument("--github-env", default="")
    validate.set_defaults(func=cmd_validate_release)

    available = subcommands.add_parser("check-availability")
    available.add_argument("version")
    available.add_argument("--repo", default="")
    available.set_defaults(func=cmd_check_availability)

    previous = subcommands.add_parser("resolve-previous-url")
    previous.add_argument("--provided-url", default="")
    previous.add_argument("--repo", default="")
    previous.add_argument("--output-file", required=True)
    previous.add_argument("--github-output", default="")
    previous.set_defaults(func=cmd_resolve_previous_url)

    bump = subcommands.add_parser("run-bump-check")
    bump.add_argument("--mode", required=True, choices=["warn", "require", "off"])
    bump.add_argument("--version", default="")
    bump.add_argument("--entrypoint", required=True)
    bump.add_argument("--previous-url", default="")
    bump.add_argument("--output-file", required=True)
    bump.add_argument("--dry-run", choices=["true", "false"], default="false")
    bump.set_defaults(func=cmd_run_bump_check)

    bundles = subcommands.add_parser("prepare-bundles")
    bundles.add_argument("--bundle-glob", required=True)
    bundles.add_argument("--bundle-manifest-path", default="")
    bundles.add_argument("--test-os-json", required=True)
    bundles.add_argument("--workspace", default="")
    bundles.add_argument("--bundle-dir", required=True)
    bundles.add_argument("--matrix-file", required=True)
    bundles.add_argument("--release-list-file", required=True)
    bundles.add_argument("--github-output", default="")
    bundles.set_defaults(func=cmd_prepare_bundles)

    github_output = subcommands.add_parser("append-github-output")
    github_output.add_argument("--github-output", required=True)
    github_output.add_argument("--name", required=True)
    github_output.add_argument("--value", required=True)
    github_output.set_defaults(func=cmd_append_github_output)

    notes = subcommands.add_parser("make-release-notes")
    notes.add_argument("--repo", default="")
    notes.add_argument("--version", default="")
    notes.add_argument("--target", default="")
    notes.add_argument("--bump-output", required=True)
    notes.add_argument("--output-file", required=True)
    notes.add_argument("--docs-url", default="")
    notes.set_defaults(func=cmd_make_release_notes)

    publish = subcommands.add_parser("publish-release")
    publish.add_argument("--version", default="")
    publish.add_argument("--repo", default="")
    publish.add_argument("--target", default="")
    publish.add_argument("--notes-file", required=True)
    publish.add_argument("--bundle-dir", required=True)
    publish.add_argument("--release-list-file", required=True)
    publish.add_argument("--check-availability", choices=["true", "false"], default="true")
    publish.set_defaults(func=cmd_publish_release)

    snapshot_docs = subcommands.add_parser("snapshot-docs")
    snapshot_docs.add_argument("--docs-root", required=True)
    snapshot_docs.add_argument("--snapshot-file", required=True)
    snapshot_docs.set_defaults(func=cmd_snapshot_docs)

    docs_index = subcommands.add_parser("write-docs-index")
    docs_index.add_argument("--docs-root", required=True)
    docs_index.add_argument("--docs-version", default="")
    docs_index.add_argument("--repo-name", default="")
    docs_index.add_argument("--update-prerelease-index", choices=["true", "false"], default="false")
    docs_index.add_argument("--github-output", default="")
    docs_index.set_defaults(func=cmd_write_docs_index)

    validate_docs = subcommands.add_parser("validate-docs")
    validate_docs.add_argument("--docs-root", required=True)
    validate_docs.add_argument("--docs-version", default="")
    validate_docs.add_argument("--snapshot-file", required=True)
    validate_docs.set_defaults(func=cmd_validate_docs)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except ReleaseError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


def cmd_validate_release(args: argparse.Namespace) -> int:
    dry_run = args.dry_run == "true"
    version_text = args.release_version
    used_dry_run_version = False
    if not version_text and dry_run:
        version_text = args.dry_run_version
        used_dry_run_version = True
    if not version_text:
        raise ReleaseError("release_version is required unless dry_run is true")

    release_version = parse_release_version(version_text)
    docs_version = parse_release_version(args.docs_version) if args.docs_version else release_version

    if args.check_availability != "true":
        print("Skipping tag/release availability check because check_availability is false.")
    elif used_dry_run_version:
        print(
            "Skipping tag/release availability check for synthetic dry-run version "
            f"{release_version.full}."
        )
    else:
        check_availability(release_version.full, args.repo)

    is_prerelease = "true" if release_version.is_prerelease else "false"
    if args.github_output:
        outputs = {
            "release_version": release_version.full,
            "docs_version": docs_version.full,
            "release_base_version": release_version.base,
            "is_dry_run": "true" if dry_run else "false",
            "is_prerelease": is_prerelease,
        }
        for name, value in outputs.items():
            append_github_output(args.github_output, name, value)
    if args.github_env:
        env_values = {
            "RELEASE_VERSION": release_version.full,
            "RELEASE_BASE_VERSION": release_version.base,
            "IS_PRERELEASE": is_prerelease,
            "DOCS_VERSION": docs_version.full,
        }
        for name, value in env_values.items():
            append_github_output(args.github_env, name, value)
    return 0


def cmd_check_availability(args: argparse.Namespace) -> int:
    check_availability(args.version, args.repo)
    return 0


def check_availability(version_text: str, repo_text: str) -> None:
    version = validate_release_version(version_text)
    repo = require_repo(env_fallback(repo_text, "GITHUB_REPOSITORY"))

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
    # GitHub also answers 404 for repos the token cannot see (or typo'd repo
    # names), which would make this check pass vacuously.
    require_repo_accessible(repo)


def cmd_resolve_previous_url(args: argparse.Namespace) -> int:
    if args.provided_url:
        previous_url = require_single_line(args.provided_url, "previous release URL")
        write_previous_url(args, previous_url)
        return 0

    repo = require_repo(env_fallback(args.repo, "GITHUB_REPOSITORY"))
    latest = run(["gh", "api", f"repos/{repo}/releases/latest"])
    if latest.returncode != 0:
        if is_github_not_found(latest):
            # Distinguish "no releases yet" from an inaccessible or typo'd
            # repo, which GitHub also reports as 404.
            require_repo_accessible(repo)
            write_previous_url(args, "")
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
        if str(asset.get("name", "")).endswith(BUNDLE_SUFFIX)
    ]
    matches = [url for url in matches if url]
    if len(matches) > 1:
        raise ReleaseError(
            f"latest release has multiple {BUNDLE_SUFFIX} assets; set previous_release_url explicitly"
        )

    previous_url = matches[0] if matches else ""
    if previous_url:
        previous_url = require_single_line(previous_url, "previous release URL")
    write_previous_url(args, previous_url)
    return 0


def write_previous_url(args: argparse.Namespace, previous_url: str) -> None:
    write_text(args.output_file, previous_url + "\n")
    if args.github_output:
        append_github_output(args.github_output, "previous_url", previous_url)


def cmd_run_bump_check(args: argparse.Namespace) -> int:
    version_text = env_fallback(args.version, "RELEASE_VERSION")
    release_version = parse_release_version(version_text)
    mode = args.mode

    if args.dry_run == "true":
        write_text(
            args.output_file,
            f"{BUMP_SKIP_PREFIX} for dry-run release {release_version.full}.\n",
        )
        return 0

    if mode == "off":
        write_text(args.output_file, f"{BUMP_SKIP_PREFIX} because bump_check is off.\n")
        return 0

    if not args.previous_url:
        if mode == "require":
            raise ReleaseError(
                "bump_check is 'require' but no previous release bundle URL was resolved; "
                "set previous_release_url explicitly, or use bump_check 'warn' or 'off' "
                "for a first release"
            )
        write_text(args.output_file, f"{BUMP_NO_PREVIOUS_PREFIX} roc bump check skipped.\n")
        return 0

    result = run(
        [
            "roc",
            "bump",
            "--old",
            args.previous_url,
            "--expect",
            release_version.base,
            args.entrypoint,
        ]
    )
    output = trim_output(result)
    if not output:
        output = "roc bump completed without output."
    write_text(args.output_file, output.rstrip() + "\n")

    if result.returncode == 0:
        return 0

    message = f"roc bump failed for expected version {release_version.base}."
    if mode == "warn":
        print(f"warning: {message}", file=sys.stderr)
        print(output, file=sys.stderr)
        return 0

    print(output, file=sys.stderr)
    raise ReleaseError(message)


def cmd_prepare_bundles(args: argparse.Namespace) -> int:
    workspace = Path(env_fallback(args.workspace, "GITHUB_WORKSPACE") or ".").resolve()
    bundle_dir = safe_workspace_output_path(args.bundle_dir, workspace, "bundle_dir")
    matrix_file = safe_workspace_output_path(args.matrix_file, workspace, "matrix_file")
    release_list_file = safe_workspace_output_path(
        args.release_list_file, workspace, "release_list_file"
    )

    glob_paths = find_bundle_glob(args.bundle_glob, workspace)
    if not glob_paths:
        raise ReleaseError(f"bundle_glob matched no files: {args.bundle_glob}")

    for path in glob_paths:
        if path.is_relative_to(bundle_dir):
            raise ReleaseError("bundle_dir must not contain files matched by bundle_glob")

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

    if bundle_dir.exists() and not bundle_dir.is_dir():
        raise ReleaseError(f"bundle_dir exists but is not a directory: {bundle_dir}")
    if bundle_dir.is_dir():
        entries = list(bundle_dir.iterdir())
        if entries and not (bundle_dir / BUNDLE_DIR_MARKER).is_file():
            raise ReleaseError(
                "bundle_dir already exists and was not created by prepare-bundles; "
                f"refusing to delete it: {bundle_dir}"
            )
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / BUNDLE_DIR_MARKER).write_text("", encoding="utf-8")

    for bundle in bundles:
        source = bundle["path"]
        artifact_file = validate_artifact_filename(source.name)
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


def cmd_append_github_output(args: argparse.Namespace) -> int:
    append_github_output(args.github_output, args.name, args.value)
    return 0


def cmd_make_release_notes(args: argparse.Namespace) -> int:
    version = validate_release_version(env_fallback(args.version, "RELEASE_VERSION"))
    repo = require_repo(env_fallback(args.repo, "GITHUB_REPOSITORY"))
    target = require_target(env_fallback(args.target, "GITHUB_SHA"))
    docs_url = require_single_line(args.docs_url, "docs URL") if args.docs_url else ""
    result = run(
        [
            "gh",
            "api",
            f"repos/{repo}/releases/generate-notes",
            "--method",
            "POST",
            "-f",
            f"tag_name={version}",
            "-f",
            f"target_commitish={target}",
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
            body += "\n\n## Roc API Changes\n\n" + markdown_code_block(bump_text)

    if docs_url:
        body += f"\n\n## Docs\n\n- [View docs for {version}]({docs_url})"

    write_text(args.output_file, body.rstrip() + "\n")
    return 0


def cmd_publish_release(args: argparse.Namespace) -> int:
    release_version = parse_release_version(env_fallback(args.version, "RELEASE_VERSION"))
    version = release_version.full
    repo = require_repo(env_fallback(args.repo, "GITHUB_REPOSITORY"))
    target = require_target(env_fallback(args.target, "GITHUB_SHA"))
    notes_file = require_nonempty_file(args.notes_file, "release notes file")
    assets = release_assets(args.bundle_dir, args.release_list_file)

    if args.check_availability == "true":
        check_availability(version, repo)

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
    create_release = [
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
    ]
    if release_version.is_prerelease:
        create_release.append("--prerelease")
    run_required(create_release, f"could not create GitHub release {version!r}")
    return 0


def cmd_snapshot_docs(args: argparse.Namespace) -> int:
    docs_root = Path(args.docs_root)
    versions = sorted(version_dirs(docs_root))
    write_json(args.snapshot_file, versions)
    return 0


def cmd_write_docs_index(args: argparse.Namespace) -> int:
    docs_version_text = env_fallback(args.docs_version, "DOCS_VERSION", "RELEASE_VERSION")
    if not docs_version_text:
        raise ReleaseError(
            "docs version is required (pass --docs-version or set DOCS_VERSION/RELEASE_VERSION)"
        )
    release_version = parse_release_version(docs_version_text)
    docs_version = release_version.full
    if release_version.is_prerelease and args.update_prerelease_index != "true":
        print(f"Skipping docs index update for prerelease docs version {docs_version}.")
        return 0
    docs_root = Path(args.docs_root)
    repo_name = clean_repo_name(env_fallback(args.repo_name, "GITHUB_REPOSITORY"))
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
    index_file = docs_root / "index.html"
    write_text(index_file, content)
    if args.github_output:
        append_github_output(args.github_output, "index_file", str(index_file))
    return 0


def cmd_validate_docs(args: argparse.Namespace) -> int:
    docs_version_text = env_fallback(args.docs_version, "DOCS_VERSION", "RELEASE_VERSION")
    if not docs_version_text:
        raise ReleaseError(
            "docs version is required (pass --docs-version or set DOCS_VERSION/RELEASE_VERSION)"
        )
    docs_version = validate_release_version(docs_version_text)
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

    before = set(read_json_file(args.snapshot_file, "docs snapshot file"))
    after = set(version_dirs(docs_root))
    missing = sorted(version for version in before if version != docs_version and version not in after)
    if missing:
        raise ReleaseError(
            "docs generation removed historical version directories: " + ", ".join(missing)
        )

    return 0


def validate_release_version(version: str) -> str:
    return parse_release_version(version).full


def parse_release_version(version: str) -> ReleaseVersion:
    if not version:
        raise ReleaseError("release version is required")
    match = RELEASE_VERSION_RE.fullmatch(version)
    if not match:
        raise ReleaseError(
            "release version must be semver X.Y.Z with optional prerelease, "
            f"without a leading v or build metadata: {version!r}"
        )
    parts = (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )
    if parts == (0, 0, 0):
        raise ReleaseError("release version 0.0.0 is reserved and cannot be published")
    return ReleaseVersion(
        full=version,
        base=match.group("base"),
        prerelease=match.group("prerelease") or "",
    )


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
    # Escape the workspace prefix so glob metacharacters in the checkout path
    # itself (for example "build [linux-2]") are matched literally.
    matches = glob.glob(os.path.join(glob.escape(str(workspace)), pattern), recursive=True)
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
        name = "default" if len(paths) == 1 else path.name.removesuffix(BUNDLE_SUFFIX)
        bundles.append({"name": name, "path": path, "test_os": test_os})
    return bundles


def validate_artifact_filename(artifact_file: str) -> str:
    require_single_line(artifact_file, "release artifact filename")
    if "/" in artifact_file or "\\" in artifact_file or Path(artifact_file).name != artifact_file:
        raise ReleaseError(f"release artifact filename must not contain directories: {artifact_file}")
    if "#" in artifact_file:
        # gh release create parses "path#label", so '#' would split the
        # filename into a path and a display label at publish time.
        raise ReleaseError(f"release artifact filename must not contain '#': {artifact_file}")
    if not artifact_file.endswith(BUNDLE_SUFFIX):
        raise ReleaseError(
            f"release artifact filename must end with {BUNDLE_SUFFIX}: {artifact_file}"
        )
    return artifact_file


def safe_existing_file(path_text: str | Path, workspace: Path) -> Path:
    text = str(path_text)
    if "\n" in text or "\r" in text:
        raise ReleaseError(f"path contains a newline: {text!r}")
    path = Path(text)
    candidate = path if path.is_absolute() else workspace / path
    resolved = candidate.resolve()
    workspace_resolved = workspace.resolve()
    if not resolved.is_relative_to(workspace_resolved):
        raise ReleaseError(f"path escapes the workspace: {text}")
    if not resolved.is_file():
        raise ReleaseError(f"file does not exist: {text}")
    return resolved


def version_dirs(docs_root: Path) -> list[str]:
    if not docs_root.is_dir():
        return []
    return [
        child.name
        for child in docs_root.iterdir()
        if child.is_dir() and is_release_version_dir(child.name)
    ]


def is_release_version_dir(name: str) -> bool:
    try:
        parse_release_version(name)
        return True
    except ReleaseError:
        return False


def clean_repo_name(repo_name: str) -> str:
    if "/" in repo_name:
        repo_name = repo_name.rsplit("/", 1)[1]
    if not repo_name or "\n" in repo_name or "\r" in repo_name or "/" in repo_name:
        raise ReleaseError(f"invalid repository name for docs redirect: {repo_name!r}")
    return repo_name


def is_meaningful_bump_output(text: str) -> bool:
    if not text:
        return False
    skipped_prefixes = (BUMP_SKIP_PREFIX, BUMP_NO_PREVIOUS_PREFIX)
    return not any(text.startswith(prefix) for prefix in skipped_prefixes)


def markdown_code_block(text: str, info: str = "text") -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n{text}\n{fence}"


def env_fallback(value: str, *env_names: str) -> str:
    if value:
        return value
    for name in env_names:
        env_value = os.environ.get(name, "")
        if env_value:
            return env_value
    return ""


def require_repo(repo: str) -> str:
    if not repo or "/" not in repo or "\n" in repo or "\r" in repo:
        raise ReleaseError("GITHUB_REPOSITORY or --repo must be set to owner/name")
    return repo


def require_repo_accessible(repo: str) -> None:
    result = run(["gh", "api", f"repos/{repo}"])
    if result.returncode != 0:
        raise ReleaseError(
            f"cannot access repository {repo!r}; check the repository name and token "
            f"permissions: {trim_output(result)}"
        )


def require_target(target: str) -> str:
    if not target or "\n" in target or "\r" in target:
        raise ReleaseError("GITHUB_SHA or --target must be set to a commit-ish")
    return target


def require_single_line(value: str, description: str) -> str:
    if "\n" in value or "\r" in value:
        raise ReleaseError(f"{description} must not contain newlines")
    return value


def require_nonempty_file(path_text: str | Path, description: str) -> Path:
    path = Path(path_text)
    if not path.is_file():
        raise ReleaseError(f"{description} is missing: {path}")
    if path.stat().st_size == 0:
        raise ReleaseError(f"{description} is empty: {path}")
    return path


def safe_workspace_output_path(path_text: str | Path, workspace: Path, description: str) -> Path:
    text = require_single_line(str(path_text), description)
    if not text:
        raise ReleaseError(f"{description} is required")

    path = Path(text)
    candidate = path if path.is_absolute() else workspace / path
    resolved = candidate.resolve(strict=False)
    workspace_resolved = workspace.resolve()

    if not resolved.is_relative_to(workspace_resolved):
        raise ReleaseError(f"{description} must resolve inside the workspace: {text}")
    if resolved == workspace_resolved:
        raise ReleaseError(f"{description} must not be the workspace root")
    if resolved == Path(resolved.anchor).resolve():
        raise ReleaseError(f"{description} must not be the filesystem root")
    if resolved == Path.home().resolve():
        raise ReleaseError(f"{description} must not be the home directory")

    return resolved


def release_assets(bundle_dir_text: str | Path, release_list_file_text: str | Path) -> list[Path]:
    bundle_dir = Path(bundle_dir_text)
    if not bundle_dir.is_dir():
        raise ReleaseError(f"release bundle directory is missing: {bundle_dir}")

    require_nonempty_file(release_list_file_text, "release bundle list file")
    release_list = read_json_file(release_list_file_text, "release bundle list file")
    if not isinstance(release_list, list) or not release_list:
        raise ReleaseError("release bundle list file must be a non-empty JSON array")

    seen: set[str] = set()
    assets: list[Path] = []
    for index, item in enumerate(release_list):
        if not isinstance(item, dict):
            raise ReleaseError(f"release bundle list entry {index} must be an object")
        artifact_file = item.get("artifact_file")
        if not isinstance(artifact_file, str) or not artifact_file:
            raise ReleaseError(f"release bundle list entry {index} has invalid artifact_file")
        validate_artifact_filename(artifact_file)
        if artifact_file in seen:
            raise ReleaseError(f"duplicate release artifact filename: {artifact_file}")
        seen.add(artifact_file)
        assets.append(bundle_dir / artifact_file)

    for asset in assets:
        if not asset.is_file():
            raise ReleaseError(f"release asset is missing: {asset}")
    return assets


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_required(command: list[str], message: str) -> None:
    result = run(command)
    if result.returncode != 0:
        raise ReleaseError(f"{message}: {trim_output(result)}")


def is_github_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode == 0:
        return False
    # gh api prints the API's JSON error body on stdout; prefer its status
    # field over substring-matching arbitrary output.
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and str(payload.get("status", "")) == "404":
        return True
    return "HTTP 404" in result.stderr


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


def read_json_file(path: str | Path, description: str) -> Any:
    try:
        return read_json(path)
    except FileNotFoundError as err:
        raise ReleaseError(f"{description} is missing: {path}") from err
    except json.JSONDecodeError as err:
        raise ReleaseError(f"{description} is not valid JSON: {err}") from err


def compact_json(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def append_github_output(path: str | Path, name: str, value: str) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
        raise ReleaseError(f"invalid GitHub output name: {name!r}")
    require_single_line(value, f"GitHub output {name}")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


if __name__ == "__main__":
    sys.exit(main())
