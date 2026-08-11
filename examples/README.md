# Tieru Public Examples

These examples use Tieru's existing version-1 YAML format. They contain no
credentials and can be inspected before use.

| Example | Best for | External credentials |
|---|---|---|
| [Local only](local-only/README.md) | New users and a simple verified local setup | None |
| [Local multi-model](local-multi-model/README.md) | Multiple explicitly described Ollama candidates | None |
| [Local plus cloud](local-plus-cloud/README.md) | Local-first routing with one optional hosted candidate | `ANTHROPIC_API_KEY` |
| [Privacy first](privacy-first/README.md) | Strict local model routing with deny-first action authorization | None |
| [Repository agent](repository-agent/README.md) | A governed developer workflow after adding a reviewed repository tool | Tool-dependent |
| [Public demo fixture](demo/README.md) | Three isolated, synthetic product demonstrations | None |

To inspect an example without replacing your project configuration:

```bash
tieru --config examples/local-only/config.yaml doctor
tieru --config examples/local-only/config.yaml
```

Global options such as `--config` come before the subcommand. If you copy an
example to `.tieru/config.yaml`, first preserve any existing configuration;
`tieru init` is the safer starting point for a new project.

The older `mcp.demo.json` and `mcp_demo_server.py` files demonstrate a tiny MCP
connector. MCP configuration is executable configuration: review it like code,
and expect Trust authorization before server startup or tool calls.

The comprehensive advanced reference remains
[`tieru/tieru.example.yaml`](../tieru/tieru.example.yaml).
