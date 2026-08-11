# Local Only

This is the smallest reference configuration for Tieru's verified keyless path.
It uses Ollama with `gemma4:e2b` for every required role and keeps Fabric on
`local_only`.

Requirements:

- Ollama installed and running
- `ollama pull gemma4:e2b`
- no API key

Shadow and browser automation are disabled. No cloud candidate, gateway, MCP
server, or custom Trust rule is configured.

```bash
tieru --config examples/local-only/config.yaml doctor
tieru --config examples/local-only/config.yaml
```

For a normal first run, `tieru init` creates the equivalent automatically
discovered project configuration.
