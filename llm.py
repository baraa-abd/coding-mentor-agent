import json
import os
import uuid
import urllib.request
import urllib.error
from dataclasses import dataclass, field


DEFAULT_URL   = "http://localhost:11434"
DEFAULT_MODEL = "qwen3-coder:30b"

ANTHROPIC_API_URL   = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"


@dataclass
class ToolSchema:
    """Tool definition passed to the model."""
    name: str
    description: str
    parameters: dict  # JSON Schema


@dataclass
class ToolCall:
    """A tool invocation returned by the model."""
    id: str
    name: str
    arguments: dict


@dataclass
class ToolResult:
    """Result of a tool execution, returned to the model."""
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class LLMResponse:
    """Response from the model."""
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class OllamaClient:
    """Wraps the Ollama HTTP API."""

    def __init__(self, model: str = DEFAULT_MODEL, base_url: str = DEFAULT_URL):
        self._model    = model
        self._base_url = base_url.rstrip("/")
        self._endpoint = f"{self._base_url}/v1/chat/completions"

    def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[ToolSchema] | None = None,
        stream: bool = True,
    ) -> LLMResponse:
        full_messages = [{"role": "system", "content": system}] + messages

        payload: dict = {
            "model":    self._model,
            "messages": full_messages,
            "stream":   stream,
        }
        if tools:
            payload["tools"] = self._serialize_tools(tools)

        if stream:
            return self._stream_complete(payload)
        else:
            return self._blocking_complete(payload)

    def _stream_complete(self, payload: dict) -> LLMResponse:
        full_text = ""
        tc_accum: dict[int, dict] = {}

        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8").strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    delta = chunk.get("choices", [{}])[0].get("delta", {})

                    text_piece = delta.get("content") or ""
                    if text_piece:
                        print(text_piece, end="", flush=True)
                        full_text += text_piece

                    for tc_delta in delta.get("tool_calls") or []:
                        idx = tc_delta.get("index", 0)
                        if idx not in tc_accum:
                            tc_accum[idx] = {"id": "", "name": "", "arguments": ""}
                        entry = tc_accum[idx]
                        if tc_delta.get("id"):
                            entry["id"] += tc_delta["id"]
                        fn = tc_delta.get("function") or {}
                        if fn.get("name"):
                            entry["name"] += fn["name"]
                        if fn.get("arguments"):
                            entry["arguments"] += fn["arguments"]

        except urllib.error.URLError as exc:
            raise ConnectionError(
                f"Cannot reach Ollama at {self._base_url}. "
                f"Is it running?  Error: {exc}"
            ) from exc

        print()

        return LLMResponse(text=full_text, tool_calls=self._finalise_tool_calls(tc_accum))

    def _blocking_complete(self, payload: dict) -> LLMResponse:
        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.URLError as exc:
            raise ConnectionError(
                f"Cannot reach Ollama at {self._base_url}. "
                f"Is it running?  Error: {exc}"
            ) from exc

        message = data.get("choices", [{}])[0].get("message", {})
        text    = message.get("content") or ""

        tc_accum: dict[int, dict] = {}
        for tc in message.get("tool_calls") or []:
            idx = tc.get("index", len(tc_accum))
            fn  = tc.get("function") or {}
            tc_accum[idx] = {
                "id":        tc.get("id", ""),
                "name":      fn.get("name", ""),
                "arguments": fn.get("arguments", "{}"),
            }

        return LLMResponse(text=text, tool_calls=self._finalise_tool_calls(tc_accum))

    @staticmethod
    def _finalise_tool_calls(accum: dict[int, dict]) -> list[ToolCall]:
        result = []
        for idx in sorted(accum):
            entry = accum[idx]
            name  = entry.get("name", "").strip()
            if not name:
                continue
            raw_args = entry.get("arguments", "{}").strip() or "{}"
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                arguments = {"_raw": raw_args}
            tc_id = entry.get("id") or str(uuid.uuid4())
            result.append(ToolCall(id=tc_id, name=name, arguments=arguments))
        return result

    @staticmethod
    def _serialize_tools(tools: list[ToolSchema]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name":        t.name,
                    "description": t.description,
                    "parameters":  t.parameters,
                },
            }
            for t in tools
        ]


class AnthropicClient:
    """
    Wraps the Anthropic Messages API (claude-* models).

    Authentication: reads ANTHROPIC_API_KEY from the environment.
    Streaming: supported and enabled by default; tokens are printed as they
               arrive, matching the behaviour of OllamaClient.

    Tool use: fully supported.  The harness passes ToolResult objects back
    via the messages list; AnthropicClient converts them to Anthropic's
    tool_result content blocks before sending.

    Message format translation
    --------------------------
    The harness keeps a flat list[dict] of messages in OpenAI format:
      {"role": "user"|"assistant"|"tool", "content": str | list}

    Anthropic uses a slightly different schema:
    - No separate "tool" role; tool results are sent as a "user" message
      with content type "tool_result".
    - Assistant tool-use turns have content blocks of type "tool_use".
    - Consecutive messages with the same role must be merged.

    This client handles all of that transparently so the harness never
    needs to know which backend is in use.
    """

    def __init__(
        self,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        api_key: str | None = None,
    ):
        self._model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "Anthropic API key not found. "
                "Set the ANTHROPIC_API_KEY environment variable or pass api_key=."
            )

    # ------------------------------------------------------------------
    # Public interface — identical signature to OllamaClient.complete()
    # ------------------------------------------------------------------

    def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[ToolSchema] | None = None,
        stream: bool = True,
    ) -> LLMResponse:
        import time 
        time.sleep(15) # Temporary workaround for Anthropic's rate limits during testing; remove in production.
        anthropic_messages = self._convert_messages(messages)

        payload: dict = {
            "model":      self._model,
            "max_tokens": 8096,
            "system":     system,
            "messages":   anthropic_messages,
        }
        if tools:
            payload["tools"] = self._serialize_tools(tools)
        if stream:
            payload["stream"] = True

        if stream:
            return self._stream_complete(payload)
        else:
            return self._blocking_complete(payload)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def _stream_complete(self, payload: dict) -> LLMResponse:
        full_text = ""
        # tool_use blocks accumulate by index: {index: {"id", "name", "input_str"}}
        tc_accum: dict[int, dict] = {}

        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            ANTHROPIC_API_URL,
            data=body,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         self._api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8").strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str in ("[DONE]", ""):
                        continue

                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type", "")

                    # Text delta
                    if event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            piece = delta.get("text", "")
                            if piece:
                                print(piece, end="", flush=True)
                                full_text += piece
                        elif delta.get("type") == "input_json_delta":
                            idx = event.get("index", 0)
                            if idx in tc_accum:
                                tc_accum[idx]["input_str"] += delta.get("partial_json", "")

                    # Start of a new content block
                    elif event_type == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "tool_use":
                            idx = event.get("index", len(tc_accum))
                            tc_accum[idx] = {
                                "id":        block.get("id", str(uuid.uuid4())),
                                "name":      block.get("name", ""),
                                "input_str": "",
                            }

        except urllib.error.URLError as exc:
            raise ConnectionError(
                f"Cannot reach Anthropic API. Error: {exc}"
            ) from exc
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read()
            raise ConnectionError(
                f"Anthropic API error {exc.code}: {body_bytes.decode()}"
            ) from exc

        print()
        return LLMResponse(
            text=full_text,
            tool_calls=self._finalise_tool_calls(tc_accum),
        )

    # ------------------------------------------------------------------
    # Blocking (non-streaming)
    # ------------------------------------------------------------------

    def _blocking_complete(self, payload: dict) -> LLMResponse:
        body = json.dumps(payload).encode()
        req  = urllib.request.Request(
            ANTHROPIC_API_URL,
            data=body,
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         self._api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read()
            raise ConnectionError(
                f"Anthropic API error {exc.code}: {body_bytes.decode()}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(
                f"Cannot reach Anthropic API. Error: {exc}"
            ) from exc

        full_text = ""
        tc_accum: dict[int, dict] = {}

        for idx, block in enumerate(data.get("content", [])):
            btype = block.get("type")
            if btype == "text":
                full_text += block.get("text", "")
            elif btype == "tool_use":
                tc_accum[idx] = {
                    "id":        block.get("id", str(uuid.uuid4())),
                    "name":      block.get("name", ""),
                    "input_str": json.dumps(block.get("input", {})),
                }

        return LLMResponse(
            text=full_text,
            tool_calls=self._finalise_tool_calls(tc_accum),
        )

    # ------------------------------------------------------------------
    # Message format conversion: OpenAI-style → Anthropic-style
    # ------------------------------------------------------------------

    def _convert_messages(self, messages: list[dict]) -> list[dict]:
        """
        Convert the harness's OpenAI-format message list to Anthropic format.

        Key differences handled here:
        - "tool" role → merged into the next "user" turn as tool_result blocks.
        - Assistant messages that contain tool calls need content blocks of
          type "tool_use" rather than a plain text string.
        - Consecutive same-role messages are merged to satisfy Anthropic's
          strict alternating-role requirement.
        """
        converted: list[dict] = []

        for msg in messages:
            role    = msg["role"]
            content = msg.get("content", "")

            if role == "tool":
                # Tool results are user-turn content blocks in Anthropic's schema.
                tool_result_block = {
                    "type":        "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content":     content if isinstance(content, str) else json.dumps(content),
                }
                # Merge into the last message if it's already a user turn,
                # otherwise open a new user turn.
                if converted and converted[-1]["role"] == "user":
                    last_content = converted[-1]["content"]
                    if isinstance(last_content, str):
                        converted[-1]["content"] = (
                            [{"type": "text", "text": last_content}]
                            if last_content else []
                        )
                    converted[-1]["content"].append(tool_result_block)
                else:
                    converted.append({"role": "user", "content": [tool_result_block]})

            elif role == "assistant":
                # Build content blocks for the assistant turn.
                blocks: list[dict] = []
                if isinstance(content, str) and content:
                    blocks.append({"type": "text", "text": content})
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            blocks.append(block)
                        elif isinstance(block, str) and block:
                            blocks.append({"type": "text", "text": block})

                # Append tool_use blocks for any tool calls stored in the message.
                for tc in msg.get("tool_calls", []):
                    fn   = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"_raw": args}
                    blocks.append({
                        "type":  "tool_use",
                        "id":    tc.get("id", str(uuid.uuid4())),
                        "name":  fn.get("name", ""),
                        "input": args,
                    })

                if not blocks:
                    # Anthropic rejects empty assistant content — skip.
                    continue

                # Merge into the previous assistant turn if possible.
                if converted and converted[-1]["role"] == "assistant":
                    existing = converted[-1]["content"]
                    if isinstance(existing, str):
                        converted[-1]["content"] = (
                            [{"type": "text", "text": existing}] if existing else []
                        )
                    converted[-1]["content"].extend(blocks)
                else:
                    converted.append({"role": "assistant", "content": blocks})

            else:  # "user"
                # Plain user text.
                if isinstance(content, str):
                    user_blocks: list[dict] | str = content
                else:
                    user_blocks = content  # already a list of blocks

                if converted and converted[-1]["role"] == "user":
                    # Merge to maintain alternating roles.
                    last_content = converted[-1]["content"]
                    if isinstance(last_content, str) and isinstance(user_blocks, str):
                        converted[-1]["content"] = last_content + "\n" + user_blocks
                    else:
                        if isinstance(last_content, str):
                            converted[-1]["content"] = (
                                [{"type": "text", "text": last_content}]
                                if last_content else []
                            )
                        if isinstance(user_blocks, str):
                            converted[-1]["content"].append(
                                {"type": "text", "text": user_blocks}
                            )
                        else:
                            converted[-1]["content"].extend(user_blocks)
                else:
                    converted.append({"role": "user", "content": user_blocks})

        return converted

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _finalise_tool_calls(accum: dict[int, dict]) -> list[ToolCall]:
        result = []
        for idx in sorted(accum):
            entry     = accum[idx]
            name      = entry.get("name", "").strip()
            if not name:
                continue
            raw_input = entry.get("input_str", "{}").strip() or "{}"
            try:
                arguments = json.loads(raw_input)
            except json.JSONDecodeError:
                arguments = {"_raw": raw_input}
            result.append(ToolCall(id=entry["id"], name=name, arguments=arguments))
        return result

    @staticmethod
    def _serialize_tools(tools: list[ToolSchema]) -> list[dict]:
        """Convert ToolSchema list to Anthropic's tool definition format."""
        return [
            {
                "name":         t.name,
                "description":  t.description,
                "input_schema": t.parameters,  # Anthropic uses input_schema, not parameters
            }
            for t in tools
        ]