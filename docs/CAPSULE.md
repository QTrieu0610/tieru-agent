# Tieru Capsule

Tieru Capsule is a local, selective, versioned snapshot of Tieru-owned identity state. It moves a persona, Memory, Memory Graph, user skills, non-secret preferences, conservative Trust configuration, and Model Fabric configuration between installations without an account, network, API key, or model call.

A Capsule is not a raw home-directory backup, credential vault, live synchronization link, remote backup service, marketplace package, or permission grant. Deleting a `.tieru` file never deletes source or imported Tieru state.

## Format and manifest

Format v1 is a ZIP-compatible `.tieru` container identified by structure and `manifest.json`, not its extension. Stable paths use `identity/`, `memory/`, `skills/`, `config/`, `trust/`, and optional `replay/`, `forge/`, and `shadow/`. SQLite rows are exported as deterministic, ID-ordered JSON instead of copying live `state.db`.

The manifest declares `tieru-capsule`, format/schema versions, a random content-independent Capsule ID, creation time, source Tieru/Python/platform versions, profile, scopes, counts, compression/encryption state, compatibility, and the byte size plus SHA-256 of every payload. SHA-256 detects corruption; it does not authenticate origin. Unknown future versions are rejected. V1 is unencrypted: password encryption is unsupported rather than implemented with custom cryptography.

Entries and manifest paths must be normalized relative POSIX paths. Inspection rejects absolute, Windows-absolute, backslash, `..`, duplicate/case-colliding, directory, and symlink entries; undeclared or missing members; malformed JSON; bad sizes/hashes; suspicious compression ratios; and archives beyond file-count, per-file, archive, or total-uncompressed bounds. Tieru never calls `extractall()`.

## Profiles and scopes

`portable` is the default:

- `identity`: user-owned `SOUL.md`, `IDENTITY.md`, and `PREFERENCES.md` when present.
- `memory`: facts, episodes, Graph entities/relations, IDs, time, confidence, importance, status, supersession, and provenance. `MEMORY.md` is a readable mirror.
- `skills`: valid user-owned `TIERU_HOME/skills/<id>/SKILL.md`, index, hash, version, and provenance. Packaged skills are not duplicated.
- `preferences`: an allowlisted non-secret memory/browser/profile subset.
- `trust`: policy as an explicitly inactive review artifact, with machine-path remap notices.
- `fabric`: non-secret profiles, routing policy, aliases, provider/model metadata, capabilities, preferences, weights, and local/cloud enablement.

`full-local-history` additionally includes `replay`, `forge`, and `shadow`; each can also be included explicitly. Replay remains bounded, redacted, ordered, read-only, and contains no reconstructed chain-of-thought. Forge drafts remain inactive. Shadow remains advisory and cannot install or grant. Fabric performance evidence travels only through explicitly selected Replay history.

Traces, caches, temp files, outbox, live databases, compatibility artifacts, credentials, environment variables, API keys, bearer/OAuth tokens, auth cookies/headers, passwords, private keys, and secret fields are never source inputs. Config comes from allowlists and is recursively checked for secret-bearing keys. Skills and completed payloads receive a final credential-pattern scan. Detection fails the operation.

## Preview, conflict handling, and atomicity

Inspection verifies the complete archive without mutation. Import always creates an `ImportPlan` containing validity, errors/warnings, creates, updates, skips, domain conflicts, path remaps, scopes, integrity, and Trust review. `--dry-run` stops there; execution re-verifies the file against the approved plan.

Memory is additive. Identical facts/episodes skip; distinct records preserve original and `capsule:<id>` provenance. Graph entities reuse matching type/name identities; relations re-link to destination IDs, retain original provenance, and restore internal supersession. Skills are parsed again: identical ID/hash skips, differing content is a `keep-existing` conflict, never an overwrite. Replay/Shadow primary-key collisions skip. Forge collisions keep the existing draft and imports remain inactive.

Trust is never merged or activated. It lands in `TIERU_HOME/capsule/pending-trust/` for review/remapping, so a Capsule cannot grant itself authority. Preferences/Fabric use current-installation-wins recursive precedence; imports fill missing keys. Credentials stay local, so cloud candidates remain unavailable until separately configured, while valid `local_only` remains preserved.

Before mutation Tieru creates `TIERU_HOME/backups/import_<id>/` through SQLite's backup API plus the active config when present. The five newest import backups are retained. Files are prepared in controlled staging and moved atomically; DB changes use one transaction. Failure rolls back rows, removes created files, and restores overwritten config. Backups are local and are not bundled.

Archive paths are relative. Tieru-home files map to destination home. Arbitrary external files are not exported. Absolute Trust path strings are marked `requires-remap`; Trust remains inactive. Dashboard actions are same-origin/CSRF protected, confined to `TIERU_HOME/capsules`, and import requires explicit confirmation.

## CLI and dashboard

```text
tieru capsule export my-tieru.tieru
tieru capsule export history.tieru --profile full-local-history
tieru capsule export selected.tieru --include replay --dry-run
tieru capsule inspect my-tieru.tieru
tieru capsule import my-tieru.tieru --dry-run
tieru capsule import my-tieru.tieru
```

The dashboard **Capsule** view exports portable state, exposes opt-in history, verifies local files, renders the ImportPlan, and requires a confirmed action. There is no upload or cloud-sync UI.

Completed operations add only safe local metadata to `capsule_audits`: IDs, time, scopes, basename, counts, and warnings. Contents and secrets are not copied into audit or Replay.

## Limitations

V1 has no encryption, signing/authenticity, cloud sync, accounts, live links, remote upload, automatic Trust activation, destructive replacement mode, or arbitrary external-path remapping UI. A Capsule can still contain private identity/history; store and transmit it accordingly.
