# Maintainer Release Process

This is a manual checklist, not release automation. Do not publish, tag, or push
until the release milestone explicitly authorizes it.

## Versioning

Tieru follows semantic-versioning intent while pre-1.0:

- patch increments are for compatible fixes and documentation;
- minor increments may add public behavior or intentionally revise a pre-1.0
  contract, with migration and compatibility notes;
- any incompatible CLI, configuration, persistence, Trust, or Capsule change
  must be conspicuous in the changelog and release notes.

The first public-beta candidate uses package version `0.3.0b1`. Its planned
GitHub-facing tag is `v0.3.0-beta.1`; the tag must point to the exact source
commit that passes hosted CI. Mark the GitHub release as a prerelease. Do not
create the tag or release from an unpushed or unverified worktree.

## Release checklist

1. Confirm the R4.1 readiness review is PASS.
2. Verify the intended commit on hosted Windows, Ubuntu, and macOS CI.
3. Confirm the package version and planned tag still match the reviewed candidate.
4. Finalize [CHANGELOG.md](../CHANGELOG.md) and the beta release notes.
5. Run `python -m pytest -q`, `python -m tieru.ops.release_gate`, and
   `python scripts/public_beta_acceptance.py`.
6. Inspect `git status`, the complete diff, built artifact contents, metadata,
   and secret/runtime-state scans.
7. Create the consolidated release commit after review.
8. Create the reviewed version tag.
9. Push the commit and tag.
10. Create a GitHub prerelease from that exact tag and attach only reviewed artifacts.

Never publish from a dirty or unreviewed worktree. Do not reuse local `dist/`
files without rebuilding and revalidating them from the release commit. Package
publishing automation is intentionally out of scope until a later reviewed milestone.
