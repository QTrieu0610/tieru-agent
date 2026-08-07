"""Provider-specific integrations outside the model protocol adapters."""

from tieru.providers.ollama import OllamaError, OllamaIntegration

__all__ = ["OllamaError", "OllamaIntegration"]
