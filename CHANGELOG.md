# Changelog

This file records user-visible Tieru changes from the public-beta line forward.
It does not attempt to reconstruct every historical upstream commit.

## Unreleased

The changes below form the planned `0.3.0b1` public-beta candidate. Keep them
under Unreleased until the exact commit has passed hosted CI and the prerelease
is actually created; then add the release date without inventing history.

### Added

- Local-first Tieru identity and canonical `tieru` package, CLI, configuration,
  and runtime directory contracts.
- Memory Graph, Trust Kernel, Replay, Skill Forge, Shadow, Model Fabric, and
  Capsule public-beta subsystems.
- Conservative Init and share-safe Doctor onboarding flows.
- Public examples, multi-OS CI definitions, and wheel/sdist fresh-install acceptance.
- A reproducible public demo guide with isolated synthetic fixtures for Trust,
  Replay, Shadow, Forge, Model Fabric, Memory, and Capsule demonstrations.

### Changed

- Public documentation now distinguishes local defaults, optional cloud
  providers, model capability metadata, and Trust authority.
- Packaging assertions cover runtime assets, bundled skills, examples, and
  forbidden local-state artifacts.

### Security

- Tool execution, gateways, MCP startup, browser actions, Capsule import, and
  external writes use explicit Trust and scope boundaries.
- Runtime records and community reporting guidance are credential- and privacy-aware.

### Documentation

- Added contributor, issue, pull-request, security, community, acceptance, and
  maintainer release guidance for the public beta.
