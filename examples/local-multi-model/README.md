# Local Multi-model

This example demonstrates Model Fabric with two explicit Ollama candidates while
preserving `local_only` routing.

`gemma4:e2b` is Tieru's verified local candidate. `YOUR_SECOND_OLLAMA_MODEL` is
an example placeholder, not a certified model. Replace it with an installed
Ollama tag and describe its capabilities accurately. In particular, do not mark
tool calling as supported unless you have verified it; the example leaves that
capability `unknown`.

```bash
tieru --config examples/local-multi-model/config.yaml doctor
tieru --config examples/local-multi-model/config.yaml fabric models
tieru --config examples/local-multi-model/config.yaml fabric status
```

Doctor may report the placeholder missing until you replace it and obtain the
selected model yourself. Tieru does not download models.
