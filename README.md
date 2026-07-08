[![Roc-Lang][roc_badge]][roc_link]

[roc_badge]: https://img.shields.io/endpoint?url=https%3A%2F%2Fpastebin.com%2Fraw%2FcFzuCCd7
[roc_link]: https://github.com/roc-lang/roc

# release-package

Composite GitHub Actions for Roc package and platform releases.

`release-package` owns release mechanics:

- strict `X.Y.Z` release validation
- tag and GitHub release availability checks
- previous bundle lookup for `roc bump`
- bundle manifest validation and test matrix generation
- release notes generation
- semver tag and GitHub release creation
- versioned docs safety checks

Caller repositories own their jobs and environments: Roc, Zig, Rust, Node, Nix,
system packages, caches, services, runner labels, artifacts, and Pages deploys.
There is no reusable workflow wrapper.

Install Zig only if your repo builds a Zig platform or otherwise needs Zig
during validation. Normal packages using prebuilt platform hosts do not need it.

## Pinning

Use actions directly from caller workflow steps:

```yaml
- uses: roc-lang/release-package/actions/prepare-bundles@<release-package-ref>
```

For release automation, prefer immutable refs: a release tag or a full commit
SHA. Use `@main` only for experiments where a moving ref is acceptable.

## Actions

- `validate-release`: validates release/docs versions and optionally checks tag/release availability
- `resolve-previous-release`: resolves an explicit or latest previous `.tar.zst` URL
- `run-bump-check`: runs `roc bump` with caller-installed Roc
- `prepare-bundles`: validates/copies bundles and outputs `test_matrix` plus `release_bundles`
- `test-bundle`: runs a caller command against one downloaded bundle
- `make-release-notes`: runs custom release notes command or generates default GitHub notes
- `publish-release`: creates the semver tag and GitHub release
- `docs-snapshot`: snapshots existing docs versions before generation
- `docs-index`: writes the docs root redirect/index
- `docs-validate`: validates generated docs and preserved historical docs

`validate-release` supports PR validation with `dry_run: true`. If no
`release_version` is provided in dry-run mode, it uses `999.999.999` and skips
availability checks for that synthetic version; when a real `release_version`
is provided, availability is still checked even in dry-run mode so duplicate
versions fail before merge. It outputs `is_dry_run`, `release_base_version`,
and `is_prerelease`, so `1.2.3-rc1` can publish as `1.2.3-rc1` while release
availability checks still use the full prerelease tag and bump checks use
`1.2.3`. `run-bump-check` writes a skipped bump output in dry-run
mode without calling `roc`.

Actions that call the GitHub API or `gh` accept `github_token`; pass
`${{ github.token }}` or provide `GH_TOKEN`/`GITHUB_TOKEN` in the job environment.

Command inputs such as `test_bundle_command` and `release_notes_command` execute
as shell on the runner with the job environment. Treat them as trusted workflow
code only. Do not derive these strings from pull request text, issue comments,
release metadata, or untrusted workflow inputs.

## Permissions

| Workflow use | Permissions |
| --- | --- |
| Build or validate only | `contents: read` |
| Availability checks or previous-release lookup | `contents: read` plus a GitHub token available to `gh` |
| Default release notes (`make-release-notes` without a custom command) | `contents: write` (the `generate-notes` API requires write access) |
| Publish GitHub release | `contents: write` |
| Commit docs and deploy Pages | `contents: write`, `pages: write`, `id-token: write` |

Prefer job-level permissions when only one job needs write access.

## Minimal Package Release

This template is for a Roc package using prebuilt platform hosts. It installs
Roc only. It validates PRs with dry-run release settings and publishes only from
manual `workflow_dispatch` runs.

```yaml
name: Release

on:
  pull_request:
  workflow_dispatch:
    inputs:
      release_version:
        description: Release version, for example 1.2.3 or 1.0.0-rc1
        required: true

permissions:
  contents: read

# Serialize release runs in one group; PR validation runs get per-ref groups
# so they never cancel a queued release run.
concurrency:
  group: ${{ github.event_name == 'workflow_dispatch' && format('release-{0}', github.repository) || format('release-validate-{0}-{1}', github.repository, github.ref) }}
  cancel-in-progress: ${{ github.event_name != 'workflow_dispatch' }}

jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      release_version: ${{ steps.validate.outputs.release_version }}
      docs_version: ${{ steps.validate.outputs.docs_version }}
      test_matrix: ${{ steps.bundles.outputs.test_matrix }}
    steps:
      - uses: actions/checkout@v4

      - uses: roc-lang/setup-roc@<setup-roc-ref>
        with:
          version: nightly-new-compiler

      - id: validate
        uses: roc-lang/release-package/actions/validate-release@<release-package-ref>
        with:
          release_version: ${{ github.event_name == 'workflow_dispatch' && inputs.release_version || '' }}
          dry_run: ${{ github.event_name != 'workflow_dispatch' }}
          github_token: ${{ github.token }}

      - run: ./ci/all_tests.sh

      - id: previous
        if: ${{ github.event_name == 'workflow_dispatch' }}
        uses: roc-lang/release-package/actions/resolve-previous-release@<release-package-ref>
        with:
          github_token: ${{ github.token }}

      - uses: roc-lang/release-package/actions/run-bump-check@<release-package-ref>
        with:
          release_version: ${{ steps.validate.outputs.release_version }}
          dry_run: ${{ github.event_name != 'workflow_dispatch' }}
          previous_url: ${{ steps.previous.outputs.previous_url }}
          bump_check: require
          bump_entrypoint: main.roc

      - run: ./scripts/bundle.sh --output-dir dist

      - id: bundles
        uses: roc-lang/release-package/actions/prepare-bundles@<release-package-ref>
        with:
          bundle_glob: dist/*.tar.zst
          test_os_json: '["ubuntu-latest"]'

      - uses: actions/upload-artifact@v4
        with:
          name: release-bundles
          path: .release/bundles/*
          if-no-files-found: error

      - uses: actions/upload-artifact@v4
        with:
          name: release-metadata
          path: |
            .release/bump-output.txt
            .release/previous-url.txt
            .release/release-bundles.json
            .release/test-matrix.json
          if-no-files-found: error

  test-bundles:
    needs: build
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        include: ${{ fromJson(needs.build.outputs.test_matrix) }}
    steps:
      - uses: actions/checkout@v4

      - uses: roc-lang/setup-roc@<setup-roc-ref>
        with:
          version: nightly-new-compiler

      - uses: actions/download-artifact@v4
        with:
          name: release-bundles
          path: .release/test-bundles

      - uses: roc-lang/release-package/actions/test-bundle@<release-package-ref>
        with:
          test_bundle_command: python3 ci/test_bundle_examples.py --bundle-path
          bundle_path: .release/test-bundles/${{ matrix.artifact_file }}
          bundle_name: ${{ matrix.bundle_name }}
          release_version: ${{ needs.build.outputs.release_version }}

  publish:
    if: ${{ github.event_name == 'workflow_dispatch' }}
    needs:
      - build
      - test-bundles
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4

      - uses: actions/download-artifact@v4
        with:
          name: release-bundles
          path: .release/bundles

      - uses: actions/download-artifact@v4
        with:
          name: release-metadata
          path: .release

      - uses: roc-lang/release-package/actions/make-release-notes@<release-package-ref>
        with:
          release_version: ${{ needs.build.outputs.release_version }}
          github_token: ${{ github.token }}

      - uses: roc-lang/release-package/actions/publish-release@<release-package-ref>
        with:
          release_version: ${{ needs.build.outputs.release_version }}
          github_token: ${{ github.token }}
```

## Platform Release

Platform repos often need Zig, Rust, C toolchains, system packages, caches, or
host-build steps. Keep those setup choices in the caller workflow before the
release-package actions.

```yaml
name: Platform Release

on:
  pull_request:
  workflow_dispatch:
    inputs:
      release_version:
        description: Release version, for example 0.3.0 or 1.0.0-rc1
        required: true

permissions:
  contents: read

# Serialize release runs in one group; PR validation runs get per-ref groups
# so they never cancel a queued release run.
concurrency:
  group: ${{ github.event_name == 'workflow_dispatch' && format('release-{0}', github.repository) || format('release-validate-{0}-{1}', github.repository, github.ref) }}
  cancel-in-progress: ${{ github.event_name != 'workflow_dispatch' }}

jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      release_version: ${{ steps.validate.outputs.release_version }}
      docs_version: ${{ steps.validate.outputs.docs_version }}
      test_matrix: ${{ steps.bundles.outputs.test_matrix }}
    steps:
      - uses: actions/checkout@v4

      - uses: roc-lang/setup-roc@<setup-roc-ref>
        with:
          version: nightly-new-compiler

      - uses: mlugg/setup-zig@<setup-zig-ref>
        with:
          version: "0.16.0"

      - run: ./ci/setup_platform_build.sh

      - id: validate
        uses: roc-lang/release-package/actions/validate-release@<release-package-ref>
        with:
          release_version: ${{ github.event_name == 'workflow_dispatch' && inputs.release_version || '' }}
          dry_run: ${{ github.event_name != 'workflow_dispatch' }}
          github_token: ${{ github.token }}

      - run: ./ci/all_tests.sh

      - id: previous
        if: ${{ github.event_name == 'workflow_dispatch' }}
        uses: roc-lang/release-package/actions/resolve-previous-release@<release-package-ref>
        with:
          github_token: ${{ github.token }}

      - uses: roc-lang/release-package/actions/run-bump-check@<release-package-ref>
        with:
          release_version: ${{ steps.validate.outputs.release_version }}
          dry_run: ${{ github.event_name != 'workflow_dispatch' }}
          previous_url: ${{ steps.previous.outputs.previous_url }}
          bump_check: require
          bump_entrypoint: platform/main.roc

      - run: ./ci/bundle_platform.sh --output-dir dist

      - id: bundles
        uses: roc-lang/release-package/actions/prepare-bundles@<release-package-ref>
        with:
          bundle_glob: dist/*.tar.zst
          bundle_manifest_path: dist/release-bundles.json

      - uses: actions/upload-artifact@v4
        with:
          name: release-bundles
          path: .release/bundles/*
          if-no-files-found: error

      - uses: actions/upload-artifact@v4
        with:
          name: release-metadata
          path: |
            .release/bump-output.txt
            .release/previous-url.txt
            .release/release-bundles.json
            .release/test-matrix.json
          if-no-files-found: error

  test-bundles:
    needs: build
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        include: ${{ fromJson(needs.build.outputs.test_matrix) }}
    steps:
      - uses: actions/checkout@v4

      - uses: roc-lang/setup-roc@<setup-roc-ref>
        with:
          version: nightly-new-compiler

      - uses: mlugg/setup-zig@<setup-zig-ref>
        with:
          version: "0.16.0"

      - run: ./ci/setup_platform_test.sh

      - uses: actions/download-artifact@v4
        with:
          name: release-bundles
          path: .release/test-bundles

      - uses: roc-lang/release-package/actions/test-bundle@<release-package-ref>
        with:
          test_bundle_command: bash ci/test_bundled_examples.sh
          bundle_path: .release/test-bundles/${{ matrix.artifact_file }}
          bundle_name: ${{ matrix.bundle_name }}
          release_version: ${{ needs.build.outputs.release_version }}

  publish:
    if: ${{ github.event_name == 'workflow_dispatch' }}
    needs:
      - build
      - test-bundles
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4

      - uses: actions/download-artifact@v4
        with:
          name: release-bundles
          path: .release/bundles

      - uses: actions/download-artifact@v4
        with:
          name: release-metadata
          path: .release

      - uses: roc-lang/release-package/actions/make-release-notes@<release-package-ref>
        with:
          release_version: ${{ needs.build.outputs.release_version }}
          github_token: ${{ github.token }}

      - uses: roc-lang/release-package/actions/publish-release@<release-package-ref>
        with:
          release_version: ${{ needs.build.outputs.release_version }}
          github_token: ${{ github.token }}
```

## Docs Publishing

Docs publishing is caller-owned. This job assumes a successful real release and
commits generated docs before deploying GitHub Pages.

```yaml
docs:
  if: ${{ github.event_name == 'workflow_dispatch' }}
  needs:
    - build
    - publish
  runs-on: ubuntu-latest
  permissions:
    contents: write
    pages: write
    id-token: write
  environment:
    name: github-pages
    url: ${{ steps.deployment.outputs.page_url }}
  steps:
    - uses: actions/checkout@v4

    - uses: roc-lang/setup-roc@<setup-roc-ref>
      with:
        version: nightly-new-compiler

    - uses: roc-lang/release-package/actions/docs-snapshot@<release-package-ref>
      with:
        docs_root: www

    - run: |
        rm -rf "www/${{ needs.build.outputs.docs_version }}"
        roc docs package/main.roc --output="www/${{ needs.build.outputs.docs_version }}"

    - uses: roc-lang/release-package/actions/docs-index@<release-package-ref>
      with:
        docs_root: www
        docs_version: ${{ needs.build.outputs.docs_version }}

    - uses: roc-lang/release-package/actions/docs-validate@<release-package-ref>
      with:
        docs_root: www
        docs_version: ${{ needs.build.outputs.docs_version }}

    - run: |
        git config user.name "github-actions[bot]"
        git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
        git add www
        if git diff --cached --quiet -- www; then
          echo "Docs did not change."
        else
          git commit -m "Update docs for ${{ needs.build.outputs.release_version }}"
          git push
        fi

    - uses: actions/configure-pages@v5

    - uses: actions/upload-pages-artifact@v3
      with:
        path: www

    - id: deployment
      uses: actions/deploy-pages@v4
```

The minimal release template exposes `release_version` and `docs_version` from
`build`, so docs jobs can depend on both `build` and `publish`: `publish` gates
deployment on a completed release, while `build` provides the version outputs.

When your workflow publishes docs, also pass `docs_url` to `make-release-notes`
so the generated notes link to them, for example
`docs_url: https://${{ github.repository_owner }}.github.io/${{ github.event.repository.name }}/${{ needs.build.outputs.docs_version }}/`.
Leave `docs_url` unset in workflows without a docs job so release notes do not
link to pages that were never published. It is fine for release notes to be
generated before the docs job deploys: the URL is deterministic and starts
working once Pages deployment finishes.

In the `publish` job, the release-notes step then looks like:

```yaml
    - uses: roc-lang/release-package/actions/make-release-notes@<release-package-ref>
      with:
        release_version: ${{ needs.build.outputs.release_version }}
        github_token: ${{ github.token }}
        docs_url: https://${{ github.repository_owner }}.github.io/${{ github.event.repository.name }}/${{ needs.build.outputs.docs_version }}/
```

## Release Candidates

Release candidates use the same manual release workflow as stable releases. To
publish an RC, trigger `workflow_dispatch` with `release_version` set to the
full prerelease version, for example `1.2.3-rc1`. The prerelease suffix is the
only RC-specific input; do not add a separate workflow flag unless your caller
repo needs extra policy.

For `1.2.3-rc1`, the actions behave this way:

- `validate-release` validates `1.2.3-rc1`, checks that the matching tag and
  GitHub release do not already exist, outputs `release_base_version=1.2.3`,
  and sets `is_prerelease=true`.
- `resolve-previous-release` still resolves GitHub's latest stable release as
  the previous bundle. `run-bump-check` passes the base version (`1.2.3`) to
  `roc bump --expect`, so the API check compares the RC against the last stable
  package rather than a previous RC.
- Bundles and bundle tests run the same way as a stable release. Test commands
  receive `RELEASE_VERSION=1.2.3-rc1`.
- `make-release-notes` writes notes for the RC tag. When `docs_url` is set, the
  notes include a direct link to the exact RC docs directory, such as
  `https://example.github.io/package/1.2.3-rc1/`.
- `publish-release` creates tag `1.2.3-rc1` and marks the GitHub release as a
  prerelease automatically.
- `docs-index` leaves the root docs redirect pointed at the current stable
  release by default. RC docs can still be committed and deployed under
  `www/1.2.3-rc1/`; users reach them from the direct release-notes link.

If you publish multiple RCs, the default bump baseline remains the latest stable
release each time. Set `previous_release_url` explicitly only for an unusual
backport, recovery run, or workflow that intentionally compares one RC against
another.

When the RC is accepted, run the same workflow with the stable version, for
example `1.2.3`. The stable release gets its own tag and GitHub release, docs
are generated under `www/1.2.3/`, and `docs-index` updates the root redirect to
the stable docs.

## Artifact Conventions

The `.release/*` paths are action defaults. Keep them unless your workflow has a
reason to override the corresponding action inputs.

The GitHub artifact names `release-bundles` and `release-metadata` are
conventions used by these examples, not required API. If you rename them, update
the matching upload and download steps together.

`prepare-bundles` writes:

- `.release/bundles/*`
- `.release/test-matrix.json`
- `.release/release-bundles.json`

`publish-release` defaults to `.release/release-bundles.json` as its asset
manifest and uploads only the listed `.tar.zst` files from `.release/bundles`.

For release candidates such as `1.2.3-rc1`, `resolve-previous-release` still
uses GitHub's latest stable release as the default previous bundle. Set
`previous_release_url` explicitly for unusual backports or recovery workflows.

With `bump_check: require`, `run-bump-check` fails when no previous release
bundle URL was resolved, so a wiring mistake cannot silently skip the check.
For a repository's first release (no previous release exists yet), use
`bump_check: warn` or `off` for that run.

## Bundle Contract

`prepare-bundles` expects one or more `.tar.zst` files matched by `bundle_glob`.
It fails if the glob matches nothing, paths escape the workspace, filenames
collide, a matched file is not a `.tar.zst`, a filename contains `#`, or any
bundle has no test runner.

Generated files are written under the workspace. `bundle_dir` must not be `/`,
`$HOME`, the workspace root, or a directory containing the source bundles matched
by `bundle_glob`. `prepare-bundles` marks the `bundle_dir` it creates and refuses
to delete an existing non-empty directory it did not create.

For a simple release, every matched bundle is tested on every runner in
`test_os_json`.

For a multi-bundle release, write a manifest and set `bundle_manifest_path`:

```json
[
  {
    "name": "default",
    "path": "dist/roc-ray-default.tar.zst",
    "test_os": ["ubuntu-latest", "macos-latest", "windows-latest"]
  },
  {
    "name": "wayland",
    "path": "dist/roc-ray-wayland.tar.zst",
    "test_os": ["ubuntu-latest"]
  }
]
```

`test-bundle` exposes:

- `BUNDLE_NAME`
- `BUNDLE_PATH` (absolute, so test scripts may change directory)
- `RELEASE_VERSION`

It also appends the absolute bundle path as the final argument to
`test_bundle_command`. Trailing whitespace in the command is trimmed, so YAML
block scalars work; a command whose last line ends in a `#` comment is
rejected because the comment would swallow the appended argument.

## Docs Contract

Docs are versioned under `docs_root`:

```text
www/
  index.html
  1.2.0/
  1.3.0-rc1/
  1.3.0/
```

The docs command should replace only the directory for the version being
published. `docs-validate` checks that `docs_root` exists, is non-empty, contains
`$DOCS_VERSION/index.html`, and did not remove older version directories that
existed before docs generation.

Release-candidate docs can be published under their exact version directory,
for example `www/1.3.0-rc1/`. By default, `docs-index` does not update the root
redirect for prerelease docs; the redirect should move only when a stable release
is published. Generated release notes can include a direct RC docs link with the
`docs_url` input on `make-release-notes`.

## Release Safety

The helpers accept stable release versions such as `1.2.3` and prerelease
versions such as `1.2.3-rc1`. For prereleases, `roc bump --expect` receives the
base version, for example `1.2.3`.

The helpers reject release versions such as:

- `v1.2.3`
- `1.2`
- `1.2.3+build.1`
- `0.0.0`

`validate-release` and `publish-release` can fail if the tag or GitHub release
already exists. `publish-release` does not overwrite releases or delete assets.
To recover from a failed or bad publish:

1. Delete the GitHub release.
2. Delete the Git tag.
3. Fix the source problem.
4. Rerun the caller workflow with the same version.
