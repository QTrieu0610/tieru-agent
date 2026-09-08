# Governed Command Runner

Tieru's `run_command` tool provides workspace-confined, policy-governed, bounded process
execution for local validation and project commands. It supports tasks such as invoking Python,
pytest, Ruff, a project build, or an installed local CLI without accepting a shell program.

Tieru Command Runner is not an OS sandbox.

## Tool contract

The model supplies structured arguments:

```json
{
  "argv": ["python", "-m", "pytest", "-q"],
  "cwd": ".",
  "timeout_seconds": 120
}
```

`argv` must be a non-empty bounded list of non-empty strings. `command`, shell syntax, stdin,
interactive PTYs, persistent shell state, and background jobs are not accepted. Tieru calls
`subprocess.Popen` with `shell=False`, `stdin=DEVNULL`, separate stdout/stderr pipes, a filtered
child environment, and an explicit timeout.

Shell interpreters including `bash`, `sh`, `zsh`, `cmd.exe`, `powershell`, and `pwsh` are denied
even when supplied explicitly in argv. Obvious privilege wrappers including `sudo`, `su`,
`doas`, and `runas` are also denied. M16 does not attempt to parse or prove the safety of every
possible executable.

## Workspace policy

The production tool uses the same resolved current workspace root as Tieru's filesystem and
coding tools. A requested `cwd` is expanded and resolved before Trust authorization. It must be
an existing directory beneath that root. Path traversal, a file used as cwd, nonexistent paths,
and symlinks resolving outside the root are rejected.

Relative executable paths must resolve inside the workspace. Absolute paths outside it are
accepted only for the current Python interpreter or when the canonical path exactly matches an
installed executable resolved from the filtered `PATH`. Missing executables are never downloaded
or installed.

Working-directory confinement does not prevent a child program from accessing other filesystem paths.

The selected cwd and invocation request are constrained; the child process is not placed in an
OS filesystem sandbox.

## Environment policy

Tieru constructs a child-only environment and never mutates `os.environ`. It retains only the
minimum cross-platform launch and locale values such as `PATH`, `HOME`/`USERPROFILE`, temporary
directory variables, locale variables, `SYSTEMROOT`/`WINDIR`, and Windows `PATHEXT`. It adds
UTF-8 and non-color output hints.

Provider API keys, gateway tokens, authorization values, generic token/secret/password variables,
and the rest of Tieru's parent environment are not inherited. M16 does not provide an API for the
model to add environment entries.

## Trust and Action Ledger

Process execution remains governed by the Trust Kernel.

`run_command` is classified conservatively as high-risk, non-read-only `process_execution` and
defaults to confirmation. It remains behind Tieru's existing experimental capability flag, so it
is not silently exposed on upgrade. Argument preparation and canonical workspace validation occur
before Trust. A denial returns before an Action Ledger claim or process launch.

After authorization, M14 atomically claims the command fingerprint. The identity contains
canonical argv, canonical cwd, bounded execution options, and a runtime-owned loop scope. Repeated
delivery of the same command within one agent turn returns the ledger result without launching a
second process. A later intentional Tieru turn receives a new internal scope and may run the same
command again. This scope is not part of the tool schema and cannot be supplied by the model.

All other M14 tools retain global semantic idempotency. In particular, external writes such as
messages and calendar changes do not gain command-style rerun behavior.

## Timeout and process termination

Timeout is mandatory: 60 seconds by default and at most 300 seconds. On POSIX, Tieru starts a new
session and terminates then kills the process group on timeout. On Windows, it creates a new
process group but stdlib-only termination is guaranteed only for the direct child; descendants
may require operating-system-specific containment beyond M16.

No interactive input is available. Tieru does not provide a terminal emulator, persistent
process, scheduler, cron facility, or background worker.

## Output, errors, and redaction

Reader threads continuously drain stdout and stderr while retaining only configured byte bounds.
The structured result includes `ok`, exit code, bounded stdout/stderr, duration, timeout state,
and truncation state. Deterministic errors include `command_invalid`, `executable_denied`,
`cwd_outside_workspace`, `cwd_not_found`, `command_timeout`, `command_not_found`,
`command_launch_error`, and `command_failed`.

Tieru applies its existing secret redaction before returning output, emitting tool events,
persisting Action Ledger results, or recording Replay previews. Full argv, environment mappings,
and unrestricted output are not added as command-specific events.

## Durable Tasks and Replay

A Durable Task step can ask the normal agent turn to call `run_command`. The path remains:

```text
Task -> Step -> run_loop -> ToolRegistry -> Trust -> Action Ledger -> run_command -> Replay
```

The structured exit/output record is normal observable tool evidence for M15 verification. The
verifier never calls `run_command`; deterministic evidence is inspected first and an optional
model verifier remains tool-free. Cancellation prevents a future task step from being claimed and
therefore prevents its command from launching.

## Network and platform limitations

Tieru does not provide OS-level network isolation for executed processes.

A child executable may use the host's network or access absolute filesystem paths if the host OS
permits it. M16 adds no container, VM, seccomp profile, Windows job-object sandbox, malware
scanner, SSH/remote execution, sudo, package installer, or privilege boundary. Tests use
`sys.executable` for cross-platform behavior and skip symlink assertions honestly where the host
does not permit symlink creation.
