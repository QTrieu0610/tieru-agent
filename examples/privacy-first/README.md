# Privacy First

This example makes the two relevant boundaries explicit:

- Model Fabric uses `local_only`, so configured model execution stays local.
- Tieru keeps the default explicit memory-write policy and deny-first Trust
  behavior.

Local model routing and action authorization are separate. A local model does
not gain permission to execute commands, write arbitrary files, use a browser,
or contact an external service. This config intentionally omits custom Trust
rules rather than adding a broad local allow policy.

```bash
tieru --config examples/privacy-first/config.yaml doctor
tieru --config examples/privacy-first/config.yaml
```

Shadow and browser automation remain disabled, and there are no cloud candidates
or credentials.
