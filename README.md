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

## Actions

Use actions directly from caller workflow steps:

```yaml
- uses: roc-lang/release-package/actions/prepare-bundles@<sha>
```

Available actions:

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

Actions that call the GitHub API or `gh` accept `github_token`; pass
`${{ github.token }}` or provide `GH_TOKEN`/`GITHUB_TOKEN` in the job environment.

## Example Caller Workflow

```yaml
name: Release

on:
  workflow_dispatch:
    inputs:
      release_version:
        description: Release version, for example 1.2.3
        required: true

permissions:
  contents: write
  pages: write
  id-token: write

concurrency:
  group: release-${{ github.repository }}
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-24.04
    outputs:
      release_version: ${{ steps.validate.outputs.release_version }}
      docs_version: ${{ steps.validate.outputs.docs_version }}
      test_matrix: ${{ steps.bundles.outputs.test_matrix }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: roc-lang/setup-roc@<sha>
        with:
          version: nightly-new-compiler

      - uses: mlugg/setup-zig@<sha>
        with:
          version: "0.16.0"

      - id: validate
        uses: roc-lang/release-package/actions/validate-release@<sha>
        with:
          release_version: ${{ inputs.release_version }}
          github_token: ${{ github.token }}

      - run: ./ci/all_tests.sh

      - id: previous
        uses: roc-lang/release-package/actions/resolve-previous-release@<sha>
        with:
          github_token: ${{ github.token }}

      - uses: roc-lang/release-package/actions/run-bump-check@<sha>
        with:
          release_version: ${{ steps.validate.outputs.release_version }}
          previous_url: ${{ steps.previous.outputs.previous_url }}
          bump_check: require
          bump_entrypoint: main.roc

      - run: ./scripts/bundle.sh --output-dir dist

      - id: bundles
        uses: roc-lang/release-package/actions/prepare-bundles@<sha>
        with:
          bundle_glob: dist/*.tar.zst

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

      - uses: roc-lang/setup-roc@<sha>
        with:
          version: nightly-new-compiler

      - uses: mlugg/setup-zig@<sha>
        with:
          version: "0.16.0"

      - uses: actions/download-artifact@v4
        with:
          name: release-bundles
          path: .release/test-bundles

      - uses: roc-lang/release-package/actions/test-bundle@<sha>
        with:
          test_bundle_command: python3 ci/test_bundle_examples.py --bundle-path
          bundle_path: .release/test-bundles/${{ matrix.artifact_file }}
          bundle_name: ${{ matrix.bundle_name }}
          release_version: ${{ needs.build.outputs.release_version }}

  publish:
    needs:
      - build
      - test-bundles
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/download-artifact@v4
        with:
          name: release-bundles
          path: .release/bundles

      - uses: actions/download-artifact@v4
        with:
          name: release-metadata
          path: .release

      - uses: roc-lang/release-package/actions/make-release-notes@<sha>
        with:
          release_version: ${{ needs.build.outputs.release_version }}
          github_token: ${{ github.token }}

      - uses: roc-lang/release-package/actions/publish-release@<sha>
        with:
          release_version: ${{ needs.build.outputs.release_version }}
          github_token: ${{ github.token }}
```

Docs publishing is also caller-owned. A docs job can compose the docs helpers with
the standard Pages actions:

```yaml
- uses: roc-lang/release-package/actions/docs-snapshot@<sha>
  with:
    docs_root: www
- run: roc docs package/main.roc --output="www/${{ needs.build.outputs.docs_version }}"
- uses: roc-lang/release-package/actions/docs-index@<sha>
  with:
    docs_root: www
    docs_version: ${{ needs.build.outputs.docs_version }}
- uses: roc-lang/release-package/actions/docs-validate@<sha>
  with:
    docs_root: www
    docs_version: ${{ needs.build.outputs.docs_version }}
- uses: actions/configure-pages@v5
- uses: actions/upload-pages-artifact@v3
  with:
    path: www
- uses: actions/deploy-pages@v4
```

## Bundle Contract

`prepare-bundles` expects one or more `.tar.zst` files matched by `bundle_glob`.
It fails if the glob matches nothing, paths escape the workspace, filenames
collide, or any bundle has no test runner.

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
- `BUNDLE_PATH`
- `RELEASE_VERSION`

It also appends `bundle_path` as the final argument to `test_bundle_command`.

## Docs Contract

Docs are versioned under `docs_root`:

```text
www/
  index.html
  1.2.0/
  1.3.0/
```

The docs command should replace only the directory for the version being
published. `docs-validate` checks that `docs_root` exists, is non-empty, contains
`$DOCS_VERSION/index.html`, and did not remove older version directories that
existed before docs generation.

## Release Safety

The helpers reject release versions such as:

- `v1.2.3`
- `1.2`
- `1.2.3-rc1`
- `0.0.0`

`validate-release` and `publish-release` can fail if the tag or GitHub release
already exists. `publish-release` does not overwrite releases or delete assets.
To recover from a failed or bad publish:

1. Delete the GitHub release.
2. Delete the Git tag.
3. Fix the source problem.
4. Rerun the caller workflow with the same `X.Y.Z` version.
