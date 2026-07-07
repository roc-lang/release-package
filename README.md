# release-package

A reusable GitHub Actions workflow for building, testing, and publishing Roc package and platform releases.

The workflow owns the common release mechanics:

- validate strict `X.Y.Z` release versions
- set up Roc by default
- run caller-owned setup, validation, bundle, and bundle-test commands
- verify the requested version with `roc bump`
- test uploaded bundles on a runner matrix
- create the bare semver Git tag and GitHub release
- publish versioned docs to GitHub Pages

Caller repositories still own their build and test details. The shared workflow does not have first-class Zig, Rust, platform-target, or example-rewrite settings.

## Caller Setup

Create a manual release workflow in the package or platform repo:

```yaml
name: Release

on:
  workflow_dispatch:
    inputs:
      release_version:
        description: Release version, for example 1.2.3
        required: true

jobs:
  release:
    uses: roc-lang/release-package/.github/workflows/roc-release.yml@main
    permissions:
      contents: write
      pages: write
      id-token: write
    with:
      release_version: ${{ inputs.release_version }}
      bump_check: require
      validate_command: ./ci/all_tests.sh
      bundle_command: ./scripts/bundle.sh --output-dir dist
      test_bundle_command: python3 ci/test_bundle_examples.py --bundle-path
      docs_command: roc docs package/main.roc --output="www/$RELEASE_VERSION"
```

For a platform repo:

```yaml
name: Release

on:
  workflow_dispatch:
    inputs:
      release_version:
        description: Release version, for example 0.3.0
        required: true

jobs:
  release:
    uses: roc-lang/release-package/.github/workflows/roc-release.yml@main
    permissions:
      contents: write
      pages: write
      id-token: write
    with:
      release_version: ${{ inputs.release_version }}
      setup_build_command: ./ci/setup_build.sh
      setup_test_command: ./ci/setup_test.sh
      validate_command: ./ci/all_tests.sh
      bundle_command: ./ci/bundle.sh --output-dir dist
      bundle_glob: dist/*.tar.zst
      test_bundle_command: bash ci/test_bundled_examples.sh
      docs_command: roc docs platform/main.roc --output="www/$RELEASE_VERSION"
```

## Inputs

Required:

- `release_version`: strict semver `X.Y.Z`, without a leading `v`
- `bundle_command`: command that creates release bundles
- `test_bundle_command`: command prefix that receives one bundle path argument

Common optional inputs:

- `roc_version`: Roc version for `roc-lang/setup-roc`, default `nightly-new-compiler`
- `roc_nightly_tag`: optional pinned `nightly-new-compiler` tag for `setup-roc`
- `setup_roc`: use `setup-roc` when no custom Roc setup command is provided, default `true`
- `setup_roc_command`: custom Roc setup command
- `setup_build_command`: build-job setup command
- `setup_test_command`: bundle-test-job setup command
- `validate_command`: source validation command, default `./ci/all_tests.sh`
- `bump_check`: `warn`, `require`, or `off`, default `warn`
- `bump_entrypoint`: entrypoint for `roc bump`, default `main.roc`
- `previous_release_url`: explicit previous bundle URL for backports or multi-bundle repos
- `bundle_glob`: release bundle glob, default `dist/*.tar.zst`
- `bundle_manifest_path`: optional multi-bundle manifest path
- `test_os_json`: default test runners when no manifest is provided, default `["ubuntu-latest","macos-latest","windows-latest"]`
- `release_notes_command`: command that writes custom notes to `$RELEASE_NOTES_PATH`
- `publish_docs`: publish versioned docs, default `true`
- `docs_command`: docs generation command, default `./docs.sh "$RELEASE_VERSION"`
- `docs_root`: docs root directory, default `www`
- `docs_version`: docs directory name, default is `release_version`
- `update_docs_index`: update `www/index.html` redirect to this release, default `true`
- `commit_docs`: commit docs changes before deploying Pages, default `true`

Command hooks run with `set -euo pipefail`. If a hook must affect later steps, write to GitHub environment files:

```sh
echo "$HOME/.local/bin" >> "$GITHUB_PATH"
echo "ROC=$PWD/roc-src/zig-out/bin/roc" >> "$GITHUB_ENV"
```

## Bundle Contract

`bundle_command` must create one or more `.tar.zst` files matched by `bundle_glob`. The workflow fails if the glob matches nothing, if paths escape the workspace, if asset filenames collide, or if any bundle has no test runner.

For a simple release, every matched bundle is tested on every runner in `test_os_json`.

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

During bundle tests, the workflow exposes:

- `BUNDLE_NAME`
- `BUNDLE_PATH`
- `RELEASE_VERSION`

It also appends `BUNDLE_PATH` as the final argument to `test_bundle_command`.

## Docs Contract

Docs are versioned under `docs_root`:

```text
www/
  index.html
  1.2.0/
  1.3.0/
```

The docs command should replace only the directory for the version being published:

```sh
rm -rf "www/$RELEASE_VERSION"
roc docs package/main.roc --output="www/$RELEASE_VERSION"
```

The workflow deploys the whole docs root so historical versions remain available. Before Pages deployment it verifies that the docs root exists, is non-empty, contains `$DOCS_VERSION/index.html`, and did not remove older version directories that existed before docs generation.

Set `publish_docs: false` for repos that do not publish docs through this workflow.

## Release Safety

The workflow rejects new releases that use:

- `v1.2.3`
- `1.2`
- `1.2.3-rc1`
- `0.0.0`

It also fails if the tag or GitHub release already exists. It does not overwrite releases or delete assets. To recover from a failed or bad release:

1. Delete the GitHub release.
2. Delete the Git tag.
3. Fix the source problem.
4. Rerun the manual release workflow with the same `X.Y.Z` version.
