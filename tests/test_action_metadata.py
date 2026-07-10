import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ACTIONS = {
    "docs-index",
    "docs-snapshot",
    "docs-validate",
    "make-release-notes",
    "prepare-bundles",
    "publish-release",
    "resolve-previous-release",
    "run-bump-check",
    "test-bundle",
    "validate-release",
}


class ActionMetadataTest(unittest.TestCase):
    def test_expected_composite_actions_exist(self):
        action_dirs = {
            path.parent.name
            for path in (ROOT / "actions").glob("*/action.yml")
        }
        self.assertEqual(action_dirs, EXPECTED_ACTIONS)

    def test_actions_are_composite_and_have_steps(self):
        for action in sorted(EXPECTED_ACTIONS):
            with self.subTest(action=action):
                text = (ROOT / "actions" / action / "action.yml").read_text(encoding="utf-8")
                self.assertIn("runs:\n  using: composite\n  steps:", text)
                self.assertIn("shell: bash", text)

    def test_old_reusable_release_workflow_is_removed(self):
        self.assertFalse((ROOT / ".github" / "workflows" / "roc-release.yml").exists())

    def test_actionlint_fixture_references_every_action(self):
        fixture = (ROOT / "tests" / "fixtures" / "composite-actions.yml").read_text(
            encoding="utf-8"
        )
        for action in sorted(EXPECTED_ACTIONS):
            with self.subTest(action=action):
                self.assertIn(f"./actions/{action}", fixture)

    def test_dry_run_action_inputs_are_documented(self):
        validate = (ROOT / "actions" / "validate-release" / "action.yml").read_text(
            encoding="utf-8"
        )
        bump = (ROOT / "actions" / "run-bump-check" / "action.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("dry_run:", validate)
        self.assertIn("dry_run_version:", validate)
        self.assertIn("is_dry_run:", validate)
        self.assertIn("release_base_version:", validate)
        self.assertIn("is_prerelease:", validate)
        self.assertIn("dry_run:", bump)
        self.assertIn("--dry-run", bump)

    def test_prerelease_related_action_inputs_are_documented(self):
        notes = (ROOT / "actions" / "make-release-notes" / "action.yml").read_text(
            encoding="utf-8"
        )
        docs_index = (ROOT / "actions" / "docs-index" / "action.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("docs_url:", notes)
        self.assertIn("update_prerelease_index:", docs_index)


if __name__ == "__main__":
    unittest.main()
