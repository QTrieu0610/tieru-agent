# Repository Agent

This example prepares local model roles and the `AGENT` execution profile for a
developer workflow. It deliberately does **not** grant repository or process
access by itself.

Before asking Tieru to inspect files or run tests, connect a reviewed tool that
is scoped to the intended repository—for example, a filesystem/process MCP
server with narrow arguments. `.tieru/mcp.json` is separate executable
configuration and is not generated here. Trust authorization is required before
an MCP process starts, and unclassified MCP tools remain denied.

After a suitable tool is configured, try prompts such as:

> Inspect this repository and summarize its structure. Do not modify files.

> Identify the relevant test command, explain it, and ask before running it.

> Run the approved tests and summarize failures without changing expectations.

Process execution may require explicit approval. This example contains no
`allow all`, unrestricted command rule, cloud candidate, browser, gateway, or
MCP server definition.

```bash
tieru --config examples/repository-agent/config.yaml doctor
tieru --config examples/repository-agent/config.yaml
```
