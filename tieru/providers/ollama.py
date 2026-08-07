"""Ollama health and discovery integration.

Inference uses the shared OpenAI-compatible adapter. Native Ollama HTTP calls
are confined here so CLI and dashboard never grow separate provider logic.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse


class OllamaError(RuntimeError):
    pass


@dataclass(frozen=True)
class OllamaModel:
    name: str
    digest: str = ""
    size: int = 0
    modified_at: str = ""


class OllamaIntegration:
    def __init__(self, openai_base_url: str, timeout: float = 5.0):
        parsed = urlparse(openai_base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise OllamaError(f"Invalid Ollama URL: {openai_base_url!r}")
        path = parsed.path.rstrip("/")
        path = path.removesuffix("/v1")
        self.native_base_url = f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")
        self.timeout = timeout

    def _get(self, path: str) -> dict:
        url = f"{self.native_base_url}{path}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            raise OllamaError(f"Ollama returned HTTP {exc.code} for {path}: {body}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise OllamaError(
                f"Cannot reach Ollama at {self.native_base_url}. "
                "Start it with 'ollama serve' and try again."
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OllamaError(f"Ollama returned invalid JSON for {path}") from exc

    def health(self) -> dict:
        data = self._get("/api/version")
        version = str(data.get("version", "")).strip()
        if not version:
            raise OllamaError("Ollama health response did not include a version")
        return {"ok": True, "version": version, "endpoint": self.native_base_url}

    def models(self) -> list[OllamaModel]:
        data = self._get("/api/tags")
        raw_models = data.get("models")
        if not isinstance(raw_models, list):
            raise OllamaError("Ollama model response did not include a models list")
        return [
            OllamaModel(
                name=str(item.get("name") or item.get("model") or ""),
                digest=str(item.get("digest", "")),
                size=int(item.get("size", 0) or 0),
                modified_at=str(item.get("modified_at", "")),
            )
            for item in raw_models
            if item.get("name") or item.get("model")
        ]

    def doctor(self, model: str) -> dict:
        health = self.health()
        models = self.models()
        installed = {item.name for item in models}
        present = model in installed or f"{model}:latest" in installed
        result = {**health, "model": model, "model_present": present,
                  "models": sorted(installed)}
        if not present:
            result["error"] = (
                f"Model '{model}' is not installed. Pull it with: ollama pull {model}"
            )
        return result
