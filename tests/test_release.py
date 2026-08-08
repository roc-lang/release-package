import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts.release import release


def parse_args(*argv):
    return release.build_parser().parse_args(list(argv))


GH_NOT_FOUND = (
    'printf \'{"message":"Not Found","documentation_url":"https://docs.github.com","status":"404"}\'\n'
    "echo 'gh: Not Found (HTTP 404)' >&2\n"
    "exit 1\n"
)

GIT_TAGS_ABSENT = (
    'if [[ "$1" == "rev-parse" ]]; then exit 1; fi\n'
    'if [[ "$1" == "ls-remote" ]]; then exit 2; fi\n'
    "exit 0\n"
)

# Repo lookups succeed (the repo is accessible); everything else is a 404.
GH_REPO_OK_RELEASES_ABSENT = (
    'if [[ "$2" == "repos/roc-lang/example" ]]; then printf \'{}\'; exit 0; fi\n' + GH_NOT_FOUND
)


class ReleaseHelpersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, path, text=""):
        target = self.tmp / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    @contextmanager
    def fake_commands(self, env=None, **scripts):
        bin_dir = self.tmp / "fake-bin"
        bin_dir.mkdir(exist_ok=True)
        for name, body in scripts.items():
            path = bin_dir / name
            path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
            path.chmod(0o755)
        overrides = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        overrides.update(env or {})
        with mock.patch.dict(os.environ, overrides):
            yield

    def prepare_args(self, **overrides):
        values = {
            "bundle_glob": "dist/*.tar.zst",
            "bundle_manifest_path": "",
            "test_os_json": '["ubuntu-latest"]',
            "workspace": str(self.tmp),
            "bundle_dir": str(self.tmp / "out"),
            "matrix_file": str(self.tmp / "matrix.json"),
            "release_list_file": str(self.tmp / "release.json"),
            "github_output": "",
        }
        values.update(overrides)
        return parse_args(
            "prepare-bundles",
            "--bundle-glob", values["bundle_glob"],
            "--bundle-manifest-path", values["bundle_manifest_path"],
            "--test-os-json", values["test_os_json"],
            "--workspace", values["workspace"],
            "--bundle-dir", values["bundle_dir"],
            "--matrix-file", values["matrix_file"],
            "--release-list-file", values["release_list_file"],
            "--github-output", values["github_output"],
        )

    # --- version parsing ---

    def test_validate_release_version_accepts_strict_semver(self):
        self.assertEqual(release.validate_release_version("1.2.3"), "1.2.3")
        self.assertEqual(release.validate_release_version("0.0.1"), "0.0.1")
        self.assertEqual(release.validate_release_version("1.2.3-rc1"), "1.2.3-rc1")
        self.assertEqual(release.validate_release_version("1.2.3-alpha.1"), "1.2.3-alpha.1")
        self.assertEqual(release.parse_release_version("1.2.3-rc1").base, "1.2.3")
        self.assertTrue(release.parse_release_version("1.2.3-rc1").is_prerelease)
        self.assertFalse(release.parse_release_version("1.2.3").is_prerelease)

    def test_validate_release_version_rejects_invalid_versions(self):
        for version in [
            "",
            "v1.2.3",
            "1.2",
            "01.2.3",
            "0.0.0",
            "1.2.3-",
            "1.2.3-01",
            "1.2.3+build.1",
            "1.2.3\n",
            " 1.2.3",
        ]:
            with self.subTest(version=version):
                with self.assertRaises(release.ReleaseError):
                    release.validate_release_version(version)

    # --- prepare-bundles ---

    def test_prepare_bundles_from_glob(self):
        bundle = self.write("dist/pkg.tar.zst", "bundle")
        output_file = self.tmp / "github-output.txt"
        args = self.prepare_args(
            test_os_json='["ubuntu-latest","macos-latest"]',
            github_output=str(output_file),
        )
        self.assertEqual(release.cmd_prepare_bundles(args), 0)

        matrix = json.loads((self.tmp / "matrix.json").read_text(encoding="utf-8"))
        self.assertEqual(
            matrix,
            [
                {"artifact_file": bundle.name, "bundle_name": "default", "os": "ubuntu-latest"},
                {"artifact_file": bundle.name, "bundle_name": "default", "os": "macos-latest"},
            ],
        )
        self.assertTrue((self.tmp / "out" / bundle.name).is_file())
        self.assertIn("test_matrix=", output_file.read_text(encoding="utf-8"))

    def test_prepare_bundles_requires_glob_match(self):
        with self.assertRaisesRegex(release.ReleaseError, "matched no files"):
            release.cmd_prepare_bundles(self.prepare_args())

    def test_prepare_bundles_rejects_output_dir_outside_workspace(self):
        self.write("dist/pkg.tar.zst", "bundle")
        args = self.prepare_args(bundle_dir=str(self.tmp.parent / "outside-bundles"))
        with self.assertRaisesRegex(release.ReleaseError, "inside the workspace"):
            release.cmd_prepare_bundles(args)

    def test_prepare_bundles_rejects_workspace_root_output_dir(self):
        self.write("dist/pkg.tar.zst", "bundle")
        args = self.prepare_args(bundle_dir=str(self.tmp))
        with self.assertRaisesRegex(release.ReleaseError, "workspace root"):
            release.cmd_prepare_bundles(args)

    def test_prepare_bundles_rejects_output_dir_containing_source_bundles(self):
        self.write("dist/pkg.tar.zst", "bundle")
        args = self.prepare_args(bundle_dir=str(self.tmp / "dist"))
        with self.assertRaisesRegex(release.ReleaseError, "must not contain"):
            release.cmd_prepare_bundles(args)

    def test_prepare_bundles_refuses_to_delete_foreign_bundle_dir(self):
        self.write("dist/pkg.tar.zst", "bundle")
        self.write(".release/previous-url.txt", "https://example.com/pkg.tar.zst")
        args = self.prepare_args(bundle_dir=str(self.tmp / ".release"))
        with self.assertRaisesRegex(release.ReleaseError, "not created by prepare-bundles"):
            release.cmd_prepare_bundles(args)
        self.assertTrue((self.tmp / ".release" / "previous-url.txt").is_file())

    def test_prepare_bundles_can_rerun_over_own_bundle_dir(self):
        self.write("dist/pkg.tar.zst", "bundle")
        self.assertEqual(release.cmd_prepare_bundles(self.prepare_args()), 0)
        self.assertEqual(release.cmd_prepare_bundles(self.prepare_args()), 0)
        self.assertTrue((self.tmp / "out" / "pkg.tar.zst").is_file())

    def test_prepare_bundles_workspace_with_glob_metacharacters(self):
        workspace = self.tmp / "build [linux-2]"
        (workspace / "dist").mkdir(parents=True)
        (workspace / "dist" / "pkg.tar.zst").write_text("bundle", encoding="utf-8")
        args = self.prepare_args(
            workspace=str(workspace),
            bundle_dir=str(workspace / "out"),
            matrix_file=str(workspace / "matrix.json"),
            release_list_file=str(workspace / "release.json"),
        )
        self.assertEqual(release.cmd_prepare_bundles(args), 0)
        self.assertTrue((workspace / "out" / "pkg.tar.zst").is_file())

    def test_prepare_bundles_rejects_non_bundle_suffix_early(self):
        self.write("dist/pkg.txt", "not a bundle")
        args = self.prepare_args(bundle_glob="dist/*")
        with self.assertRaisesRegex(release.ReleaseError, r"must end with \.tar\.zst"):
            release.cmd_prepare_bundles(args)

    def test_prepare_bundles_rejects_hash_in_artifact_filename(self):
        self.write("dist/pkg#linux.tar.zst", "bundle")
        args = self.prepare_args()
        with self.assertRaisesRegex(release.ReleaseError, "must not contain '#'"):
            release.cmd_prepare_bundles(args)

    def test_prepare_bundles_rejects_manifest_missing_glob_entry(self):
        self.write("dist/default.tar.zst", "default")
        self.write("dist/wayland.tar.zst", "wayland")
        manifest = self.write(
            "dist/release-bundles.json",
            json.dumps(
                [
                    {
                        "name": "default",
                        "path": "dist/default.tar.zst",
                        "test_os": ["ubuntu-latest"],
                    }
                ]
            ),
        )
        args = self.prepare_args(bundle_manifest_path=str(manifest.relative_to(self.tmp)))
        with self.assertRaisesRegex(release.ReleaseError, "without manifest test entries"):
            release.cmd_prepare_bundles(args)

    def test_manifest_rejects_duplicate_names_and_empty_tests(self):
        self.write("dist/a.tar.zst", "a")
        manifest = self.write(
            "dist/release-bundles.json",
            json.dumps(
                [
                    {"name": "same", "path": "dist/a.tar.zst", "test_os": ["ubuntu-latest"]},
                    {"name": "same", "path": "dist/a.tar.zst", "test_os": []},
                ]
            ),
        )
        with self.assertRaisesRegex(release.ReleaseError, "duplicate bundle name"):
            release.bundles_from_manifest(str(manifest), self.tmp)

    def test_safe_existing_file_rejects_path_traversal(self):
        outside = self.tmp.parent / "outside.tar.zst"
        outside.write_text("outside", encoding="utf-8")
        try:
            with self.assertRaisesRegex(release.ReleaseError, "escapes the workspace"):
                release.safe_existing_file("../outside.tar.zst", self.tmp)
        finally:
            outside.unlink(missing_ok=True)

    # --- docs ---

    def test_docs_validation_preserves_historical_versions(self):
        self.write("www/1.0.0/index.html", "old")
        snapshot = self.tmp / "snapshot.json"
        release.cmd_snapshot_docs(
            parse_args(
                "snapshot-docs", "--docs-root", str(self.tmp / "www"), "--snapshot-file", str(snapshot)
            )
        )
        self.write("www/2.0.0/index.html", "new")
        release.cmd_validate_docs(
            parse_args(
                "validate-docs",
                "--docs-root", str(self.tmp / "www"),
                "--docs-version", "2.0.0",
                "--snapshot-file", str(snapshot),
            )
        )

    def test_docs_validation_rejects_removed_historical_versions(self):
        self.write("www/1.0.0/index.html", "old")
        snapshot = self.tmp / "snapshot.json"
        release.cmd_snapshot_docs(
            parse_args(
                "snapshot-docs", "--docs-root", str(self.tmp / "www"), "--snapshot-file", str(snapshot)
            )
        )
        shutil.rmtree(self.tmp / "www" / "1.0.0")
        self.write("www/2.0.0/index.html", "new")
        with self.assertRaisesRegex(release.ReleaseError, "removed historical"):
            release.cmd_validate_docs(
                parse_args(
                    "validate-docs",
                    "--docs-root", str(self.tmp / "www"),
                    "--docs-version", "2.0.0",
                    "--snapshot-file", str(snapshot),
                )
            )

    def test_docs_validation_reports_missing_snapshot(self):
        self.write("www/2.0.0/index.html", "new")
        with self.assertRaisesRegex(release.ReleaseError, "docs snapshot file is missing"):
            release.cmd_validate_docs(
                parse_args(
                    "validate-docs",
                    "--docs-root", str(self.tmp / "www"),
                    "--docs-version", "2.0.0",
                    "--snapshot-file", str(self.tmp / "missing-snapshot.json"),
                )
            )

    def test_write_docs_index(self):
        release.cmd_write_docs_index(
            parse_args(
                "write-docs-index",
                "--docs-root", str(self.tmp / "www"),
                "--docs-version", "1.2.3",
                "--repo-name", "roc-ansi",
            )
        )
        index = (self.tmp / "www" / "index.html").read_text(encoding="utf-8")
        self.assertIn("/roc-ansi/1.2.3/", index)

    def test_write_docs_index_skips_prerelease_by_default(self):
        output_file = self.tmp / "github-output.txt"
        with redirect_stdout(StringIO()):
            release.cmd_write_docs_index(
                parse_args(
                    "write-docs-index",
                    "--docs-root", str(self.tmp / "www"),
                    "--docs-version", "1.2.3-rc1",
                    "--repo-name", "roc-ansi",
                    "--github-output", str(output_file),
                )
            )
        self.assertFalse((self.tmp / "www" / "index.html").exists())
        # The output must not claim a file that was never written.
        self.assertFalse(output_file.exists())

    def test_write_docs_index_can_update_prerelease_when_allowed(self):
        output_file = self.tmp / "github-output.txt"
        release.cmd_write_docs_index(
            parse_args(
                "write-docs-index",
                "--docs-root", str(self.tmp / "www"),
                "--docs-version", "1.2.3-rc1",
                "--repo-name", "roc-ansi",
                "--update-prerelease-index", "true",
                "--github-output", str(output_file),
            )
        )
        index = (self.tmp / "www" / "index.html").read_text(encoding="utf-8")
        self.assertIn("/roc-ansi/1.2.3-rc1/", index)
        self.assertIn("index_file=", output_file.read_text(encoding="utf-8"))

    # --- run-bump-check ---

    def bump_args(self, mode="require", version="1.2.3", previous_url="", dry_run="false", output=None):
        return parse_args(
            "run-bump-check",
            "--mode", mode,
            "--version", version,
            "--entrypoint", "main.roc",
            "--previous-url", previous_url,
            "--output-file", str(output or (self.tmp / "bump.txt")),
            "--dry-run", dry_run,
        )

    def test_run_bump_check_warn_does_not_fail(self):
        output = self.tmp / "bump.txt"
        with self.fake_commands(roc="echo bump failed >&2\nexit 1\n"):
            with redirect_stderr(StringIO()):
                code = release.cmd_run_bump_check(
                    self.bump_args(
                        mode="warn",
                        previous_url="https://example.com/1.2.2/pkg.tar.zst",
                        output=output,
                    )
                )
        self.assertEqual(code, 0)
        self.assertIn("bump failed", output.read_text(encoding="utf-8"))

    def test_run_bump_check_require_fails_on_bump_error(self):
        with self.fake_commands(roc="echo bump failed >&2\nexit 1\n"):
            with redirect_stderr(StringIO()):
                with self.assertRaisesRegex(release.ReleaseError, "roc bump failed"):
                    release.cmd_run_bump_check(
                        self.bump_args(previous_url="https://example.com/pkg.tar.zst")
                    )

    def test_run_bump_check_dry_run_does_not_call_roc(self):
        output = self.tmp / "bump.txt"
        with self.fake_commands(roc="echo should not run >&2\nexit 1\n"):
            code = release.cmd_run_bump_check(
                self.bump_args(
                    version="999.999.999",
                    previous_url="https://example.com/pkg.tar.zst",
                    dry_run="true",
                    output=output,
                )
            )
        self.assertEqual(code, 0)
        self.assertIn("dry-run release 999.999.999", output.read_text(encoding="utf-8"))

    def test_run_bump_check_uses_base_version_for_prerelease(self):
        log = self.tmp / "roc.log"
        with self.fake_commands(
            env={"ROC_LOG": str(log)},
            roc='printf \'%s\\n\' "$*" > "$ROC_LOG"\n',
        ):
            code = release.cmd_run_bump_check(
                self.bump_args(version="1.2.3-rc1", previous_url="https://example.com/pkg.tar.zst")
            )
        self.assertEqual(code, 0)
        self.assertIn("--expect 1.2.3 main.roc", log.read_text(encoding="utf-8"))
        self.assertNotIn("1.2.3-rc1", log.read_text(encoding="utf-8"))

    def test_run_bump_check_require_fails_without_previous_url(self):
        with self.assertRaisesRegex(release.ReleaseError, "no previous release bundle URL"):
            release.cmd_run_bump_check(self.bump_args(previous_url=""))

    def test_run_bump_check_warn_skips_without_previous_url(self):
        output = self.tmp / "bump.txt"
        code = release.cmd_run_bump_check(self.bump_args(mode="warn", previous_url="", output=output))
        self.assertEqual(code, 0)
        self.assertIn("No previous release bundle found;", output.read_text(encoding="utf-8"))

    def test_run_bump_check_version_falls_back_to_env(self):
        output = self.tmp / "bump.txt"
        with mock.patch.dict(os.environ, {"RELEASE_VERSION": "3.2.1"}):
            code = release.cmd_run_bump_check(self.bump_args(version="", dry_run="true", output=output))
        self.assertEqual(code, 0)
        self.assertIn("dry-run release 3.2.1", output.read_text(encoding="utf-8"))

    # --- resolve-previous-url ---

    def resolve_args(self, output=None, github_output=""):
        return parse_args(
            "resolve-previous-url",
            "--provided-url", "",
            "--repo", "roc-lang/example",
            "--output-file", str(output or (self.tmp / "previous-url.txt")),
            "--github-output", github_output,
        )

    def test_resolve_previous_url_uses_single_latest_asset(self):
        output = self.tmp / "previous-url.txt"
        github_output = self.tmp / "github-output.txt"
        gh = (
            "printf '{\"assets\":[{\"name\":\"pkg.tar.zst\","
            "\"browser_download_url\":\"https://example.com/pkg.tar.zst\"}]}'\n"
        )
        with self.fake_commands(gh=gh):
            release.cmd_resolve_previous_url(
                self.resolve_args(output=output, github_output=str(github_output))
            )
        self.assertEqual(output.read_text(encoding="utf-8"), "https://example.com/pkg.tar.zst\n")
        self.assertIn(
            "previous_url=https://example.com/pkg.tar.zst",
            github_output.read_text(encoding="utf-8"),
        )

    def test_resolve_previous_url_rejects_multiple_assets(self):
        gh = (
            "printf '{\"assets\":["
            "{\"name\":\"a.tar.zst\",\"browser_download_url\":\"https://example.com/a.tar.zst\"},"
            "{\"name\":\"b.tar.zst\",\"browser_download_url\":\"https://example.com/b.tar.zst\"}"
            "]}'\n"
        )
        with self.fake_commands(gh=gh):
            with self.assertRaisesRegex(release.ReleaseError, "multiple"):
                release.cmd_resolve_previous_url(self.resolve_args())

    def test_resolve_previous_url_rejects_newline_provided_url(self):
        with self.assertRaisesRegex(release.ReleaseError, "must not contain newlines"):
            release.cmd_resolve_previous_url(
                parse_args(
                    "resolve-previous-url",
                    "--provided-url", "https://example.com/pkg.tar.zst\nextra=value",
                    "--repo", "roc-lang/example",
                    "--output-file", str(self.tmp / "previous-url.txt"),
                )
            )

    def test_resolve_previous_url_treats_missing_release_as_empty(self):
        output = self.tmp / "previous-url.txt"
        with self.fake_commands(gh=GH_REPO_OK_RELEASES_ABSENT):
            self.assertEqual(release.cmd_resolve_previous_url(self.resolve_args(output=output)), 0)
        self.assertEqual(output.read_text(encoding="utf-8"), "\n")

    def test_resolve_previous_url_rejects_inaccessible_repo(self):
        # GitHub reports both missing repos and forbidden repos as 404; that
        # must not be mistaken for "no previous release exists".
        with self.fake_commands(gh=GH_NOT_FOUND):
            with self.assertRaisesRegex(release.ReleaseError, "cannot access repository"):
                release.cmd_resolve_previous_url(self.resolve_args())

    # --- check-availability ---

    def test_check_availability_passes_when_tag_and_release_are_absent(self):
        with self.fake_commands(git=GIT_TAGS_ABSENT, gh=GH_REPO_OK_RELEASES_ABSENT):
            self.assertEqual(
                release.cmd_check_availability(
                    parse_args("check-availability", "1.2.3", "--repo", "roc-lang/example")
                ),
                0,
            )

    def test_check_availability_rejects_existing_release(self):
        with self.fake_commands(git=GIT_TAGS_ABSENT, gh="printf '{}'\n"):
            with self.assertRaisesRegex(release.ReleaseError, "already exists"):
                release.cmd_check_availability(
                    parse_args("check-availability", "1.2.3", "--repo", "roc-lang/example")
                )

    def test_check_availability_rejects_inaccessible_repo(self):
        with self.fake_commands(git=GIT_TAGS_ABSENT, gh=GH_NOT_FOUND):
            with self.assertRaisesRegex(release.ReleaseError, "cannot access repository"):
                release.cmd_check_availability(
                    parse_args("check-availability", "1.2.3", "--repo", "roc-lang/example")
                )

    # --- validate-release ---

    def test_validate_release_dry_run_writes_outputs_and_skips_availability(self):
        output_file = self.tmp / "github-output.txt"
        env_file = self.tmp / "github-env.txt"
        with self.fake_commands(git="echo should not run >&2\nexit 1\n", gh="echo should not run >&2\nexit 1\n"):
            with redirect_stdout(StringIO()) as stdout:
                code = release.cmd_validate_release(
                    parse_args(
                        "validate-release",
                        "--dry-run", "true",
                        "--github-output", str(output_file),
                        "--github-env", str(env_file),
                    )
                )
        self.assertEqual(code, 0)
        self.assertIn("synthetic dry-run version", stdout.getvalue())
        outputs = output_file.read_text(encoding="utf-8")
        self.assertIn("release_version=999.999.999", outputs)
        self.assertIn("is_dry_run=true", outputs)
        self.assertIn("is_prerelease=false", outputs)
        env_values = env_file.read_text(encoding="utf-8")
        self.assertIn("RELEASE_VERSION=999.999.999", env_values)
        self.assertIn("DOCS_VERSION=999.999.999", env_values)

    def test_validate_release_checks_availability_for_real_version_in_dry_run(self):
        # A real release_version must be availability-checked even in dry-run
        # mode, so duplicate versions fail during PR validation.
        with self.fake_commands(git=GIT_TAGS_ABSENT, gh="printf '{}'\n"):
            with self.assertRaisesRegex(release.ReleaseError, "already exists"):
                release.cmd_validate_release(
                    parse_args(
                        "validate-release",
                        "--release-version", "1.2.3",
                        "--dry-run", "true",
                        "--repo", "roc-lang/example",
                    )
                )

    def test_validate_release_reports_prerelease_versions(self):
        output_file = self.tmp / "github-output.txt"
        with redirect_stdout(StringIO()):
            release.cmd_validate_release(
                parse_args(
                    "validate-release",
                    "--release-version", "1.2.3-rc1",
                    "--check-availability", "false",
                    "--github-output", str(output_file),
                )
            )
        outputs = output_file.read_text(encoding="utf-8")
        self.assertIn("release_version=1.2.3-rc1", outputs)
        self.assertIn("release_base_version=1.2.3", outputs)
        self.assertIn("is_prerelease=true", outputs)

    def test_validate_release_requires_version_without_dry_run(self):
        with self.assertRaisesRegex(release.ReleaseError, "release_version is required"):
            release.cmd_validate_release(parse_args("validate-release"))

    # --- make-release-notes ---

    def notes_args(self, version="1.2.3", bump_output=None, output=None, docs_url=""):
        return parse_args(
            "make-release-notes",
            "--repo", "roc-lang/example",
            "--version", version,
            "--target", "abc123",
            "--bump-output", str(bump_output or (self.tmp / "missing-bump.txt")),
            "--output-file", str(output or (self.tmp / "notes.md")),
            "--docs-url", docs_url,
        )

    def test_make_release_notes_appends_bump_output(self):
        bump = self.write("bump.txt", "Added Foo\n")
        output = self.tmp / "notes.md"
        with self.fake_commands(gh="printf '{\"body\":\"Generated notes\"}'\n"):
            release.cmd_make_release_notes(self.notes_args(bump_output=bump, output=output))
        notes = output.read_text(encoding="utf-8")
        self.assertIn("Generated notes", notes)
        self.assertIn("Added Foo", notes)

    def test_make_release_notes_omits_skipped_bump_output(self):
        bump = self.write("bump.txt", "roc bump check skipped because bump_check is off.\n")
        output = self.tmp / "notes.md"
        with self.fake_commands(gh="printf '{\"body\":\"Generated notes\"}'\n"):
            release.cmd_make_release_notes(self.notes_args(bump_output=bump, output=output))
        notes = output.read_text(encoding="utf-8")
        self.assertIn("Generated notes", notes)
        self.assertNotIn("Roc API Changes", notes)

    def test_make_release_notes_appends_docs_url(self):
        output = self.tmp / "notes.md"
        with self.fake_commands(gh="printf '{\"body\":\"Generated notes\"}'\n"):
            release.cmd_make_release_notes(
                self.notes_args(
                    version="1.2.3-rc1",
                    output=output,
                    docs_url="https://roc-lang.github.io/example/1.2.3-rc1/",
                )
            )
        notes = output.read_text(encoding="utf-8")
        self.assertIn("Generated notes", notes)
        self.assertIn("View docs for 1.2.3-rc1", notes)
        self.assertIn("https://roc-lang.github.io/example/1.2.3-rc1/", notes)

    def test_make_release_notes_extends_fence_for_embedded_backticks(self):
        bump = self.write("bump.txt", "Changed\n```\nexample fence\n```\n")
        output = self.tmp / "notes.md"
        with self.fake_commands(gh="printf '{\"body\":\"Generated notes\"}'\n"):
            release.cmd_make_release_notes(self.notes_args(bump_output=bump, output=output))
        notes = output.read_text(encoding="utf-8")
        self.assertIn("````text", notes)
        self.assertIn("example fence", notes)

    def test_make_release_notes_requires_repo(self):
        args = parse_args(
            "make-release-notes",
            "--repo", "",
            "--version", "1.2.3",
            "--target", "abc123",
            "--bump-output", str(self.tmp / "missing-bump.txt"),
            "--output-file", str(self.tmp / "notes.md"),
        )
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}):
            with self.assertRaisesRegex(release.ReleaseError, "GITHUB_REPOSITORY or --repo"):
                release.cmd_make_release_notes(args)

    # --- append-github-output ---

    def test_append_github_output_rejects_newline_values(self):
        output = self.tmp / "github-output.txt"
        with self.assertRaisesRegex(release.ReleaseError, "must not contain newlines"):
            release.cmd_append_github_output(
                parse_args(
                    "append-github-output",
                    "--github-output", str(output),
                    "--name", "previous_url",
                    "--value", "https://example.com/pkg.tar.zst\nextra=value",
                )
            )

    def test_append_github_output_rejects_invalid_names(self):
        output = self.tmp / "github-output.txt"
        for name in ["bad.name", "bad\nname", "name\n"]:
            with self.subTest(name=name):
                with self.assertRaisesRegex(release.ReleaseError, "invalid GitHub output name"):
                    release.append_github_output(str(output), name, "ok")

    # --- create-followup-pr ---

    def followup_args(
        self,
        version="1.2.3",
        paths="examples\nwww",
        branch_prefix="release-followup",
        base_branch="main",
        commit_message="Update docs and examples",
        pr_title="Update docs and examples",
        pr_body="",
        labels="",
        output=None,
    ):
        return parse_args(
            "create-followup-pr",
            "--release-version", version,
            "--paths", paths,
            "--branch-prefix", branch_prefix,
            "--base-branch", base_branch,
            "--commit-message", commit_message,
            "--pr-title", pr_title,
            "--pr-body", pr_body,
            "--labels", labels,
            "--repo", "roc-lang/example",
            "--github-output", str(output or (self.tmp / "followup-output.txt")),
        )

    def test_create_followup_pr_skips_when_paths_did_not_change(self):
        log = self.tmp / "commands.log"
        output = self.tmp / "followup-output.txt"
        git = (
            'echo "git:$*" >> "$LOG_PATH"\n'
            'if [[ "$1" == "diff" ]]; then exit 0; fi\n'
        )
        with self.fake_commands(
            env={"LOG_PATH": str(log)},
            git=git,
            gh="echo should not call gh >&2\nexit 1\n",
        ):
            with redirect_stdout(StringIO()):
                self.assertEqual(release.cmd_create_followup_pr(self.followup_args(output=output)), 0)
        commands = log.read_text(encoding="utf-8")
        self.assertIn("git:checkout -B release-followup/1.2.3 HEAD", commands)
        self.assertIn("git:add -A -- examples www", commands)
        self.assertNotIn("git:commit", commands)
        self.assertNotIn("git:push", commands)
        outputs = output.read_text(encoding="utf-8")
        self.assertIn("changed=false", outputs)
        self.assertIn("branch=release-followup/1.2.3", outputs)

    def test_create_followup_pr_creates_new_pr(self):
        log = self.tmp / "commands.log"
        output = self.tmp / "followup-output.txt"
        git = (
            'echo "git:$*" >> "$LOG_PATH"\n'
            'case "$1" in\n'
            "  diff) exit 1 ;;\n"
            "  rev-parse) printf 'abc123\\n'; exit 0 ;;\n"
            "  ls-remote) exit 0 ;;\n"
            "esac\n"
        )
        gh = (
            'echo "gh:$*" >> "$LOG_PATH"\n'
            'if [[ "$1" == "pr" && "$2" == "list" ]]; then printf \'[]\\n\'; exit 0; fi\n'
            'if [[ "$1" == "pr" && "$2" == "create" ]]; then '
            "printf 'https://github.com/roc-lang/example/pull/12\\n'; exit 0; fi\n"
            "exit 1\n"
        )
        with self.fake_commands(env={"LOG_PATH": str(log)}, git=git, gh=gh):
            self.assertEqual(release.cmd_create_followup_pr(self.followup_args(output=output)), 0)
        commands = log.read_text(encoding="utf-8")
        self.assertIn("git:commit -m Update docs and examples -- examples www", commands)
        self.assertIn("git:push origin HEAD:refs/heads/release-followup/1.2.3", commands)
        # `pr list --head` matches headRefName literally, so it takes the bare
        # branch; `pr create --head` takes the owner-qualified form.
        self.assertIn(
            "gh:pr list --repo roc-lang/example --state open --base main "
            "--head release-followup/1.2.3",
            commands,
        )
        self.assertIn("gh:pr create --repo roc-lang/example --base main", commands)
        self.assertIn("--head roc-lang:release-followup/1.2.3", commands)
        outputs = output.read_text(encoding="utf-8")
        self.assertIn("changed=true", outputs)
        self.assertIn("commit_sha=abc123", outputs)
        self.assertIn("pull_request_number=12", outputs)
        self.assertIn("pull_request_url=https://github.com/roc-lang/example/pull/12", outputs)

    def test_create_followup_pr_updates_existing_pr_with_force_with_lease(self):
        log = self.tmp / "commands.log"
        git = (
            'echo "git:$*" >> "$LOG_PATH"\n'
            'case "$1" in\n'
            "  diff) exit 1 ;;\n"
            "  rev-parse) printf 'def456\\n'; exit 0 ;;\n"
            "  ls-remote) printf 'oldsha\\trefs/heads/release-followup/1.2.3\\n'; exit 0 ;;\n"
            "esac\n"
        )
        gh = (
            'echo "gh:$*" >> "$LOG_PATH"\n'
            'if [[ "$1" == "pr" && "$2" == "list" ]]; then '
            'printf \'[{"number":7,"url":"https://github.com/roc-lang/example/pull/7"}]\\n\'; '
            "exit 0; fi\n"
            'if [[ "$1" == "pr" && "$2" == "edit" ]]; then exit 0; fi\n'
            "exit 1\n"
        )
        with self.fake_commands(env={"LOG_PATH": str(log)}, git=git, gh=gh):
            self.assertEqual(
                release.cmd_create_followup_pr(
                    self.followup_args(labels="release, docs", pr_body="Generated follow-up")
                ),
                0,
            )
        commands = log.read_text(encoding="utf-8")
        self.assertIn(
            "git:push --force-with-lease=refs/heads/release-followup/1.2.3:oldsha "
            "origin HEAD:refs/heads/release-followup/1.2.3",
            commands,
        )
        self.assertIn(
            "gh:pr list --repo roc-lang/example --state open --base main "
            "--head release-followup/1.2.3",
            commands,
        )
        self.assertNotIn("gh:pr create", commands)
        self.assertIn("gh:pr edit 7 --repo roc-lang/example", commands)
        self.assertIn("--body Generated follow-up", commands)
        self.assertIn("--add-label release --add-label docs", commands)

    def test_create_followup_pr_rejects_unsafe_paths(self):
        for path in ["..", "../www", "/tmp/www", ".", ":/magic", "www\\docs"]:
            with self.subTest(path=path):
                with self.assertRaises(release.ReleaseError):
                    release.cmd_create_followup_pr(self.followup_args(paths=path))

    def test_create_followup_pr_rejects_invalid_branch_prefix(self):
        with self.assertRaisesRegex(release.ReleaseError, "invalid branch prefix"):
            release.cmd_create_followup_pr(self.followup_args(branch_prefix="bad prefix"))

    # --- publish-release ---

    def publish_args(
        self,
        version="1.2.3",
        notes=None,
        bundle_dir=None,
        release_list=None,
        additional_assets="",
        check_availability="true",
    ):
        return parse_args(
            "publish-release",
            "--version", version,
            "--repo", "roc-lang/example",
            "--target", "abc123",
            "--notes-file", str(notes),
            "--bundle-dir", str(bundle_dir),
            "--release-list-file", str(release_list),
            "--additional-assets", additional_assets,
            "--check-availability", check_availability,
        )

    def test_publish_release_rechecks_availability_and_creates_release(self):
        log = self.tmp / "commands.log"
        git = (
            'case "$1" in\n'
            "  rev-parse) exit 1 ;;\n"
            "  ls-remote) exit 2 ;;\n"
            "esac\n"
            'echo "git:$*" >> "$LOG_PATH"\n'
        )
        gh = (
            'if [[ "$1" == "api" && "$2" == "repos/roc-lang/example" ]]; then printf \'{}\'; exit 0; fi\n'
            'if [[ "$1" == "api" ]]; then\n' + GH_NOT_FOUND + "fi\n"
            'echo "gh:$*" >> "$LOG_PATH"\n'
        )
        notes = self.write("release-notes.md", "Generated notes\n")
        bundle_dir = self.tmp / "bundles"
        bundle_dir.mkdir()
        self.write("bundles/pkg.tar.zst", "bundle")
        self.write("bundles/debug.txt", "not for release")
        release_list = self.write(
            "release-bundles.json",
            json.dumps(
                [
                    {
                        "name": "default",
                        "artifact_file": "pkg.tar.zst",
                        "source_path": "dist/pkg.tar.zst",
                    }
                ]
            ),
        )
        with self.fake_commands(env={"LOG_PATH": str(log)}, git=git, gh=gh):
            self.assertEqual(
                release.cmd_publish_release(
                    self.publish_args(notes=notes, bundle_dir=bundle_dir, release_list=release_list)
                ),
                0,
            )
        commands = log.read_text(encoding="utf-8")
        self.assertIn("git:tag 1.2.3 abc123", commands)
        self.assertIn("git:push origin refs/tags/1.2.3", commands)
        self.assertIn("gh:release create 1.2.3", commands)
        self.assertIn(str(bundle_dir / "pkg.tar.zst"), commands)
        self.assertNotIn("debug.txt", commands)
        self.assertIn("--notes-file", commands)

    def test_publish_release_includes_additional_assets(self):
        log = self.tmp / "commands.log"
        notes = self.write("release-notes.md", "Generated notes\n")
        bundle_dir = self.tmp / "bundles"
        bundle_dir.mkdir()
        self.write("bundles/pkg.tar.zst", "bundle")
        docs = self.write("docs.tar.gz", "docs")
        release_list = self.write(
            "release-bundles.json",
            json.dumps([{"name": "default", "artifact_file": "pkg.tar.zst"}]),
        )
        with self.fake_commands(
            env={"LOG_PATH": str(log)},
            git='echo "git:$*" >> "$LOG_PATH"\n',
            gh='echo "gh:$*" >> "$LOG_PATH"\n',
        ):
            release.cmd_publish_release(
                self.publish_args(
                    notes=notes,
                    bundle_dir=bundle_dir,
                    release_list=release_list,
                    additional_assets=f"\n{docs}\n",
                    check_availability="false",
                )
            )
        commands = log.read_text(encoding="utf-8")
        self.assertIn(f"gh:release create 1.2.3 {bundle_dir / 'pkg.tar.zst'} {docs}", commands)

    def test_publish_release_rejects_duplicate_additional_asset_filename(self):
        notes = self.write("release-notes.md", "Generated notes\n")
        bundle_dir = self.tmp / "bundles"
        bundle_dir.mkdir()
        self.write("bundles/pkg.tar.zst", "bundle")
        additional_dir = self.tmp / "additional"
        additional_dir.mkdir()
        duplicate = self.write("additional/pkg.tar.zst", "duplicate")
        release_list = self.write(
            "release-bundles.json",
            json.dumps([{"name": "default", "artifact_file": "pkg.tar.zst"}]),
        )
        with self.assertRaisesRegex(release.ReleaseError, "duplicate release asset filename"):
            release.cmd_publish_release(
                self.publish_args(
                    notes=notes,
                    bundle_dir=bundle_dir,
                    release_list=release_list,
                    additional_assets=str(duplicate),
                    check_availability="false",
                )
            )

    def test_publish_release_marks_prerelease(self):
        log = self.tmp / "commands.log"
        notes = self.write("release-notes.md", "Generated notes\n")
        bundle_dir = self.tmp / "bundles"
        bundle_dir.mkdir()
        self.write("bundles/pkg.tar.zst", "bundle")
        release_list = self.write(
            "release-bundles.json",
            json.dumps([{"name": "default", "artifact_file": "pkg.tar.zst"}]),
        )
        with self.fake_commands(
            env={"LOG_PATH": str(log)},
            git='echo "git:$*" >> "$LOG_PATH"\n',
            gh='echo "gh:$*" >> "$LOG_PATH"\n',
        ):
            release.cmd_publish_release(
                self.publish_args(
                    version="1.2.3-rc1",
                    notes=notes,
                    bundle_dir=bundle_dir,
                    release_list=release_list,
                    check_availability="false",
                )
            )
        commands = log.read_text(encoding="utf-8")
        self.assertIn("git:tag 1.2.3-rc1 abc123", commands)
        self.assertIn("gh:release create 1.2.3-rc1", commands)
        self.assertIn("--prerelease", commands)

    def test_publish_release_requires_nonempty_notes(self):
        bundle_dir = self.tmp / "bundles"
        bundle_dir.mkdir()
        self.write("bundles/pkg.tar.zst", "bundle")
        notes = self.write("release-notes.md", "")
        release_list = self.write(
            "release-bundles.json",
            json.dumps([{"name": "default", "artifact_file": "pkg.tar.zst"}]),
        )
        with self.assertRaisesRegex(release.ReleaseError, "release notes file is empty"):
            release.cmd_publish_release(
                self.publish_args(
                    notes=notes,
                    bundle_dir=bundle_dir,
                    release_list=release_list,
                    check_availability="false",
                )
            )

    def test_publish_release_requires_assets(self):
        notes = self.write("release-notes.md", "Generated notes\n")
        bundle_dir = self.tmp / "bundles"
        bundle_dir.mkdir()
        release_list = self.write(
            "release-bundles.json",
            json.dumps([{"name": "default", "artifact_file": "pkg.tar.zst"}]),
        )
        with self.assertRaisesRegex(release.ReleaseError, "release asset is missing"):
            release.cmd_publish_release(
                self.publish_args(
                    notes=notes,
                    bundle_dir=bundle_dir,
                    release_list=release_list,
                    check_availability="false",
                )
            )

    def test_publish_release_rejects_unexpected_asset_extension(self):
        notes = self.write("release-notes.md", "Generated notes\n")
        bundle_dir = self.tmp / "bundles"
        bundle_dir.mkdir()
        self.write("bundles/pkg.txt", "not a bundle")
        release_list = self.write(
            "release-bundles.json",
            json.dumps([{"name": "default", "artifact_file": "pkg.txt"}]),
        )
        with self.assertRaisesRegex(release.ReleaseError, r"must end with \.tar\.zst"):
            release.cmd_publish_release(
                self.publish_args(
                    notes=notes,
                    bundle_dir=bundle_dir,
                    release_list=release_list,
                    check_availability="false",
                )
            )


if __name__ == "__main__":
    unittest.main()
