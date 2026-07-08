import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

from scripts.release import release


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

    def test_validate_release_version_accepts_strict_semver(self):
        self.assertEqual(release.validate_release_version("1.2.3"), "1.2.3")
        self.assertEqual(release.validate_release_version("0.0.1"), "0.0.1")

    def test_validate_release_version_rejects_invalid_versions(self):
        for version in ["", "v1.2.3", "1.2", "1.2.3-rc1", "01.2.3", "0.0.0"]:
            with self.subTest(version=version):
                with self.assertRaises(release.ReleaseError):
                    release.validate_release_version(version)

    def test_prepare_bundles_from_glob(self):
        bundle = self.write("dist/pkg.tar.zst", "bundle")
        matrix_file = self.tmp / "matrix.json"
        release_file = self.tmp / "release.json"
        output_file = self.tmp / "github-output.txt"

        args = namespace(
            bundle_glob="dist/*.tar.zst",
            bundle_manifest_path="",
            test_os_json='["ubuntu-latest","macos-latest"]',
            workspace=str(self.tmp),
            bundle_dir=str(self.tmp / "out"),
            matrix_file=str(matrix_file),
            release_list_file=str(release_file),
            github_output=str(output_file),
        )
        self.assertEqual(release.cmd_prepare_bundles(args), 0)

        matrix = json.loads(matrix_file.read_text(encoding="utf-8"))
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
        args = namespace(
            bundle_glob="dist/*.tar.zst",
            bundle_manifest_path="",
            test_os_json='["ubuntu-latest"]',
            workspace=str(self.tmp),
            bundle_dir=str(self.tmp / "out"),
            matrix_file=str(self.tmp / "matrix.json"),
            release_list_file=str(self.tmp / "release.json"),
            github_output="",
        )
        with self.assertRaisesRegex(release.ReleaseError, "matched no files"):
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
        args = namespace(
            bundle_glob="dist/*.tar.zst",
            bundle_manifest_path=str(manifest.relative_to(self.tmp)),
            test_os_json='["ubuntu-latest"]',
            workspace=str(self.tmp),
            bundle_dir=str(self.tmp / "out"),
            matrix_file=str(self.tmp / "matrix.json"),
            release_list_file=str(self.tmp / "release.json"),
            github_output="",
        )
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

    def test_docs_validation_preserves_historical_versions(self):
        self.write("www/1.0.0/index.html", "old")
        snapshot = self.tmp / "snapshot.json"
        release.cmd_snapshot_docs(namespace(docs_root=str(self.tmp / "www"), snapshot_file=str(snapshot)))
        self.write("www/2.0.0/index.html", "new")
        release.cmd_validate_docs(
            namespace(
                docs_root=str(self.tmp / "www"),
                docs_version="2.0.0",
                snapshot_file=str(snapshot),
            )
        )

    def test_docs_validation_rejects_removed_historical_versions(self):
        self.write("www/1.0.0/index.html", "old")
        snapshot = self.tmp / "snapshot.json"
        release.cmd_snapshot_docs(namespace(docs_root=str(self.tmp / "www"), snapshot_file=str(snapshot)))
        shutil.rmtree(self.tmp / "www" / "1.0.0")
        self.write("www/2.0.0/index.html", "new")
        with self.assertRaisesRegex(release.ReleaseError, "removed historical"):
            release.cmd_validate_docs(
                namespace(
                    docs_root=str(self.tmp / "www"),
                    docs_version="2.0.0",
                    snapshot_file=str(snapshot),
                )
            )

    def test_write_docs_index(self):
        release.cmd_write_docs_index(
            namespace(docs_root=str(self.tmp / "www"), docs_version="1.2.3", repo_name="roc-ansi")
        )
        index = (self.tmp / "www" / "index.html").read_text(encoding="utf-8")
        self.assertIn("/roc-ansi/1.2.3/", index)

    def test_run_bump_check_warn_does_not_fail(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        roc = bin_dir / "roc"
        roc.write_text("#!/usr/bin/env bash\necho bump failed >&2\nexit 1\n", encoding="utf-8")
        roc.chmod(0o755)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        try:
            output = self.tmp / "bump.txt"
            with redirect_stderr(StringIO()):
                code = release.cmd_run_bump_check(
                    namespace(
                        mode="warn",
                        version="1.2.3",
                        entrypoint="main.roc",
                        previous_url="https://example.com/1.2.2/pkg.tar.zst",
                        output_file=str(output),
                    )
                )
            self.assertEqual(code, 0)
            self.assertIn("bump failed", output.read_text(encoding="utf-8"))
        finally:
            os.environ["PATH"] = old_path

    def test_resolve_previous_url_uses_single_latest_asset(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"assets\":[{\"name\":\"pkg.tar.zst\",\"browser_download_url\":\"https://example.com/pkg.tar.zst\"}]}'\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        try:
            output = self.tmp / "previous-url.txt"
            release.cmd_resolve_previous_url(
                namespace(provided_url="", repo="roc-lang/example", output_file=str(output))
            )
            self.assertEqual(output.read_text(encoding="utf-8"), "https://example.com/pkg.tar.zst\n")
        finally:
            os.environ["PATH"] = old_path

    def test_resolve_previous_url_rejects_multiple_assets(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"assets\":["
            "{\"name\":\"a.tar.zst\",\"browser_download_url\":\"https://example.com/a.tar.zst\"},"
            "{\"name\":\"b.tar.zst\",\"browser_download_url\":\"https://example.com/b.tar.zst\"}"
            "]}'\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        try:
            with self.assertRaisesRegex(release.ReleaseError, "multiple"):
                release.cmd_resolve_previous_url(
                    namespace(
                        provided_url="",
                        repo="roc-lang/example",
                        output_file=str(self.tmp / "previous-url.txt"),
                    )
                )
        finally:
            os.environ["PATH"] = old_path

    def test_check_availability_passes_when_tag_and_release_are_absent(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        git = bin_dir / "git"
        git.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == \"rev-parse\" ]]; then exit 1; fi\n"
            "if [[ \"$1\" == \"ls-remote\" ]]; then exit 2; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        gh = bin_dir / "gh"
        gh.write_text("#!/usr/bin/env bash\necho 'Not Found' >&2\nexit 1\n", encoding="utf-8")
        git.chmod(0o755)
        gh.chmod(0o755)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        try:
            self.assertEqual(
                release.cmd_check_availability(namespace(version="1.2.3", repo="roc-lang/example")),
                0,
            )
        finally:
            os.environ["PATH"] = old_path

    def test_check_availability_rejects_existing_release(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        git = bin_dir / "git"
        git.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == \"rev-parse\" ]]; then exit 1; fi\n"
            "if [[ \"$1\" == \"ls-remote\" ]]; then exit 2; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        gh = bin_dir / "gh"
        gh.write_text("#!/usr/bin/env bash\nprintf '{}'\n", encoding="utf-8")
        git.chmod(0o755)
        gh.chmod(0o755)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        try:
            with self.assertRaisesRegex(release.ReleaseError, "already exists"):
                release.cmd_check_availability(namespace(version="1.2.3", repo="roc-lang/example"))
        finally:
            os.environ["PATH"] = old_path

    def test_make_release_notes_appends_bump_output(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"body\":\"Generated notes\"}'\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        bump = self.write("bump.txt", "Added Foo\n")
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        try:
            output = self.tmp / "notes.md"
            release.cmd_make_release_notes(
                namespace(
                    repo="roc-lang/example",
                    version="1.2.3",
                    target="abc123",
                    bump_output=str(bump),
                    output_file=str(output),
                )
            )
            notes = output.read_text(encoding="utf-8")
            self.assertIn("Generated notes", notes)
            self.assertIn("Added Foo", notes)
        finally:
            os.environ["PATH"] = old_path

    def test_make_release_notes_omits_skipped_bump_output(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            "printf '{\"body\":\"Generated notes\"}'\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        bump = self.write("bump.txt", "roc bump check skipped because bump_check is off.\n")
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        try:
            output = self.tmp / "notes.md"
            release.cmd_make_release_notes(
                namespace(
                    repo="roc-lang/example",
                    version="1.2.3",
                    target="abc123",
                    bump_output=str(bump),
                    output_file=str(output),
                )
            )
            notes = output.read_text(encoding="utf-8")
            self.assertIn("Generated notes", notes)
            self.assertNotIn("Roc API Changes", notes)
        finally:
            os.environ["PATH"] = old_path

    def test_publish_release_rechecks_availability_and_creates_release(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        log = self.tmp / "commands.log"
        git = bin_dir / "git"
        git.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$1\" in\n"
            "  rev-parse) exit 1 ;;\n"
            "  ls-remote) exit 2 ;;\n"
            "esac\n"
            "echo \"git:$*\" >> \"$LOG_PATH\"\n",
            encoding="utf-8",
        )
        gh = bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env bash\n"
            "if [[ \"$1\" == \"api\" ]]; then echo 'Not Found' >&2; exit 1; fi\n"
            "echo \"gh:$*\" >> \"$LOG_PATH\"\n",
            encoding="utf-8",
        )
        git.chmod(0o755)
        gh.chmod(0o755)
        notes = self.write("release-notes.md", "Generated notes\n")
        bundle_dir = self.tmp / "bundles"
        bundle_dir.mkdir()
        self.write("bundles/pkg.tar.zst", "bundle")
        old_path = os.environ["PATH"]
        old_log = os.environ.get("LOG_PATH")
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        os.environ["LOG_PATH"] = str(log)
        try:
            self.assertEqual(
                release.cmd_publish_release(
                    namespace(
                        version="1.2.3",
                        repo="roc-lang/example",
                        target="abc123",
                        notes_file=str(notes),
                        bundle_dir=str(bundle_dir),
                        skip_availability_check=False,
                    )
                ),
                0,
            )
            commands = log.read_text(encoding="utf-8")
            self.assertIn("git:tag 1.2.3 abc123", commands)
            self.assertIn("git:push origin refs/tags/1.2.3", commands)
            self.assertIn("gh:release create 1.2.3", commands)
            self.assertIn(str(bundle_dir / "pkg.tar.zst"), commands)
            self.assertIn("--notes-file", commands)
        finally:
            os.environ["PATH"] = old_path
            if old_log is None:
                os.environ.pop("LOG_PATH", None)
            else:
                os.environ["LOG_PATH"] = old_log

    def test_publish_release_requires_nonempty_notes(self):
        bundle_dir = self.tmp / "bundles"
        bundle_dir.mkdir()
        self.write("bundles/pkg.tar.zst", "bundle")
        notes = self.write("release-notes.md", "")
        with self.assertRaisesRegex(release.ReleaseError, "release notes file is empty"):
            release.cmd_publish_release(
                namespace(
                    version="1.2.3",
                    repo="roc-lang/example",
                    target="abc123",
                    notes_file=str(notes),
                    bundle_dir=str(bundle_dir),
                    skip_availability_check=True,
                )
            )

    def test_publish_release_requires_assets(self):
        notes = self.write("release-notes.md", "Generated notes\n")
        bundle_dir = self.tmp / "bundles"
        bundle_dir.mkdir()
        with self.assertRaisesRegex(release.ReleaseError, "has no files"):
            release.cmd_publish_release(
                namespace(
                    version="1.2.3",
                    repo="roc-lang/example",
                    target="abc123",
                    notes_file=str(notes),
                    bundle_dir=str(bundle_dir),
                    skip_availability_check=True,
                )
            )


def namespace(**kwargs):
    return type("Args", (), kwargs)()


if __name__ == "__main__":
    unittest.main()
