"""Protocol adapters exposing one Anthropic-shaped internal messages interface."""

from __future__ import annotations

import json
from types import SimpleNamespace


class ModelError(RuntimeError):
    """A provider-neutral model failure safe to surface at the app boundary."""


def _model_error(
    protocol: str, exc: Exception, secrets: tuple[str, ...] = ()
) -> ModelError:
    name = type(exc).__name__
    message = str(exc)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return ModelError(f"{protocol} model request failed ({name}): {message}")


class AnthropicMessagesAdapter:
    """Native Anthropic Messages protocol behind Tieru's internal interface."""

    protocol = "anthropic"

    def __init__(self, api_key: str, base_url: str | None = None, timeout: float = 120.0):
        import anthropic

        self._secret = api_key
        kwargs: dict = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**kwargs)
        self.messages = SimpleNamespace(create=self._create, stream=self._stream)

    def _create(self, **kwargs):
        try:
            return self._client.messages.create(**kwargs)
        except Exception as exc:
            raise _model_error(self.protocol, exc, (self._secret,)) from exc

    def _stream(self, **kwargs):
        try:
            return self._client.messages.stream(**kwargs)
        except Exception as exc:
            raise _model_error(self.protocol, exc, (self._secret,)) from exc


class OpenAIChatAdapter:
    """OpenAI-compatible chat completions normalized to Messages responses."""

    protocol = "openai"

    def __init__(self, api_key: str, base_url: str | None = None, timeout: float = 120.0):
        import openai

        self._secret = api_key
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self.messages = SimpleNamespace(create=self._create, stream=self._stream)

    def _to_openai(self, *, model, messages, max_tokens, system=None, tools=None) -> dict:
        openai_messages = []
        if system:
            openai_messages.append({"role": "system", "content": system})
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                openai_messages.append({"role": message["role"], "content": content})
            elif message["role"] == "assistant":
                text = "".join(
                    block.text for block in content if getattr(block, "type", "") == "text"
                )
                calls = []
                for block in content:
                    if getattr(block, "type", "") != "tool_use":
                        continue
                    call = {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input),
                        },
                    }
                    if getattr(block, "extra", None):
                        call["extra_content"] = block.extra
                    calls.append(call)
                entry: dict = {"role": "assistant", "content": text or None}
                if calls:
                    entry["tool_calls"] = calls
                openai_messages.append(entry)
            else:
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": block["content"],
                        })
        kwargs: dict = {
            "model": model,
            "messages": openai_messages,
            "max_completion_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["input_schema"],
                    },
                }
                for tool in tools
            ]
        return kwargs

    def _call(self, kwargs: dict, **extra):
        try:
            return self._client.chat.completions.create(**kwargs, **extra)
        except Exception as exc:
            message = str(exc).lower()
            if "max_completion_tokens" not in message and "max_tokens" not in message:
                raise _model_error(self.protocol, exc, (self._secret,)) from exc
            fallback = dict(kwargs)
            fallback["max_tokens"] = fallback.pop("max_completion_tokens", None)
            try:
                return self._client.chat.completions.create(**fallback, **extra)
            except Exception as retry_exc:
                raise _model_error(self.protocol, retry_exc, (self._secret,)) from retry_exc

    def _create(self, *, model, messages, max_tokens, system=None, tools=None):
        response = self._call(self._to_openai(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
        ))
        if not getattr(response, "choices", None):
            error = getattr(response, "error", None) or "endpoint returned no choices"
            raise ModelError(f"{model}: {error}")
        choice = response.choices[0].message
        blocks = []
        if choice.content:
            blocks.append(SimpleNamespace(type="text", text=choice.content))
        for call in choice.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise ModelError(
                    f"Model returned invalid JSON arguments for tool '{call.function.name}'"
                ) from exc
            blocks.append(SimpleNamespace(
                type="tool_use",
                id=call.id,
                name=call.function.name,
                input=arguments,
                extra=getattr(call, "extra_content", None),
            ))
        usage = getattr(response, "usage", None)
        return SimpleNamespace(
            stop_reason="tool_use" if choice.tool_calls else "end_turn",
            usage=SimpleNamespace(
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
            ),
            content=blocks,
        )

    def _stream(self, *, model, messages, max_tokens, system=None, tools=None):
        kwargs = self._to_openai(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
        )
        return _OpenAIStream(self, kwargs)


class _OpenAIStream:
    def __init__(self, client: OpenAIChatAdapter, kwargs: dict):
        self._client = client
        self._kwargs = kwargs
        self._text: list[str] = []
        self._tools: dict[int, dict] = {}
        self._usage = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        stream = self._client._call(
            self._kwargs, stream=True, stream_options={"include_usage": True}
        )
        for chunk in stream:
            if getattr(chunk, "usage", None):
                self._usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                self._text.append(delta.content)
                yield delta.content
            for tool_call in (getattr(delta, "tool_calls", None) or []):
                slot = self._tools.setdefault(
                    tool_call.index, {"id": None, "name": "", "args": ""}
                )
                if tool_call.id:
                    slot["id"] = tool_call.id
                if tool_call.function and tool_call.function.name:
                    slot["name"] = tool_call.function.name
                if tool_call.function and tool_call.function.arguments:
                    slot["args"] += tool_call.function.arguments

    def get_final_message(self):
        blocks = []
        text = "".join(self._text)
        if text:
            blocks.append(SimpleNamespace(type="text", text=text))
        for slot in self._tools.values():
            try:
                arguments = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError as exc:
                raise ModelError(
                    f"Model returned invalid streamed JSON for tool '{slot['name']}'"
                ) from exc
            blocks.append(SimpleNamespace(
                type="tool_use",
                id=slot["id"],
                name=slot["name"],
                input=arguments,
            ))
        return SimpleNamespace(
            stop_reason="tool_use" if self._tools else "end_turn",
            usage=SimpleNamespace(
                input_tokens=getattr(self._usage, "prompt_tokens", 0),
                output_tokens=getattr(self._usage, "completion_tokens", 0),
            ),
            content=blocks,
        )
