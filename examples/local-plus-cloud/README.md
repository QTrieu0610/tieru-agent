# Local Plus Cloud

This example keeps the active roles local and adds one explicit Anthropic
candidate under the `local_first` Fabric policy. Cloud is not inferred from an
API key: both the candidate in YAML and its credential are required before it
can become eligible.

Before use:

1. Replace `YOUR_CLOUD_MODEL_ID` with a model identifier available to your account.
2. Update the candidate capability metadata to match that model.
3. Put the credential in the environment, never in YAML.

PowerShell:

```powershell
$env:ANTHROPIC_API_KEY = "YOUR_API_KEY"
```

Linux/macOS:

```bash
export ANTHROPIC_API_KEY="YOUR_API_KEY"
```

Then inspect availability and policy:

```bash
tieru --config examples/local-plus-cloud/config.yaml doctor
tieru --config examples/local-plus-cloud/config.yaml fabric models
tieru --config examples/local-plus-cloud/config.yaml fabric status
```

Missing cloud credentials do not invalidate the local path; Doctor reports the
explicit cloud candidate as unavailable or optional.
