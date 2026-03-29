import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from harness.memory_manager import MemoryManager
from llm import ToolCall, ToolResult, ToolSchema


_DANGEROUS_PATTERNS = re.compile(
    r"os\.system|subprocess|shutil\.rmtree|os\.remove|os\.unlink"
    r"|open\s*\(.*['\"]w['\"]|socket\.|urllib|requests\.|http\.",
    re.IGNORECASE,
)

_DOCKER_LANGUAGES: dict[str, dict] = {
    "python": {
        "image":     "python:latest",
        "cmd":       ["python", "/sandbox/code.py"],
        "filename":  "code.py",
        "setup_cmd": None,
    },
    "javascript": {
        "image":     "node:current-alpine3.23",
        "cmd":       ["node", "/sandbox/code.js"],
        "filename":  "code.js",
        "setup_cmd": None,
    },
    "cpp": {
        "image":     "gcc:latest",
        "cmd":       ["/sandbox/a.out"],
        "filename":  "code.cpp",
        "setup_cmd": "g++ -O1 -o /sandbox/a.out /sandbox/code.cpp && chmod +x /sandbox/a.out",
    },
}

_DOCKER_FLAGS = [
    "--rm",
    "--network", "none",
    "--memory", "512m",
    "--cpus", "0.5",
    "--read-only",
    "--tmpfs", "/sandbox:size=16m,exec,mode=777",
    "--tmpfs", "/tmp:size=8m",
    "--user", "nobody",
    "--security-opt", "no-new-privileges",
]

_EXEC_TIMEOUT = 20


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _run_in_docker(language: str, code: str) -> dict:
    cfg = _DOCKER_LANGUAGES[language]
    with tempfile.TemporaryDirectory() as host_tmp:
        host_file = Path(host_tmp) / cfg["filename"]
        host_file.write_text(code, encoding="utf-8")

        def _docker_run(override_cmd=None):
            cmd = (
                ["docker", "run"]
                + _DOCKER_FLAGS
                + [
                    "--volume", f"{host_tmp}:/host_src:ro",
                    cfg["image"],
                    "sh", "-c",
                    f"cp /host_src/{cfg['filename']} /sandbox/{cfg['filename']} && "
                    + (" && ".join([cfg["setup_cmd"]] if cfg.get("setup_cmd") else []) +
                       (" && " if cfg.get("setup_cmd") else ""))
                    + " ".join(override_cmd or [str(p) for p in cfg["cmd"]]),
                ]
            )
            return subprocess.run(
                cmd, capture_output=True, timeout=_EXEC_TIMEOUT, text=True,
            )

        try:
            result = _docker_run()
            return {
                "stdout":     result.stdout,
                "stderr":     result.stderr,
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "error":      f"Execution timed out ({_EXEC_TIMEOUT}s limit)",
                "stdout":     "",
                "stderr":     "",
                "returncode": -1,
            }


@dataclass
class RegisteredTool:
    schema: ToolSchema
    handler: callable
    calling_convention: str  # "query" | "context"


class ToolRegistry:
    """Registry of all agent tools with dispatch logic."""

    def __init__(self, memory: MemoryManager, scratchpad: dict,
                 on_end_session: callable):
        self._memory         = memory
        self._scratchpad     = scratchpad       # live reference
        self._on_end_session = on_end_session   # called when end_session tool fires
        self._tools: dict[str, RegisteredTool] = {}
        self._register_all()

    def schemas(self) -> list[ToolSchema]:
        return [rt.schema for rt in self._tools.values()]

    def dispatch(self, tool_calls: list[ToolCall], silent: bool = False) -> list[ToolResult]:
        results = []
        for call in tool_calls:
            if call.name not in self._tools:
                results.append(ToolResult(
                    tool_call_id=call.id,
                    content=f"Unknown tool: {call.name}",
                    is_error=True,
                ))
                continue

            rt = self._tools[call.name]
            is_context = rt.calling_convention == "context"

            try:
                content = rt.handler(**call.arguments)
                results.append(ToolResult(
                    tool_call_id=call.id,
                    content=str(content),
                ))
                
            except Exception as e:
                if is_context or silent:
                    results.append(ToolResult(
                        tool_call_id=call.id,
                        content=json.dumps({"result": None, "error": str(e)}),
                        is_error=False,
                    ))
                else:
                    results.append(ToolResult(
                        tool_call_id=call.id,
                        content=f"Tool error: {e}",
                        is_error=True,
                    ))
        return results

    def _register(self, name, description, parameters, handler, calling_convention):
        self._tools[name] = RegisteredTool(
            schema=ToolSchema(name=name, description=description, parameters=parameters),
            handler=handler,
            calling_convention=calling_convention,
        )

    def _register_all(self):

        self._register(
            name="read_topic_detail",
            description=(
                "Retrieve the full detail record for a topic: id, title, "
                "short_description, user_level, note. "
                "Use when: (1) about to discuss a topic — check the user's prior "
                "history and comfort level before teaching it; "
                "(2) verifying whether a topic_id exists in memory before referencing it. "
                "Do NOT call for topics already discussed in this conversation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "topic_id": {
                        "type": "string",
                        "description": "Snake_case topic identifier.",
                    }
                },
                "required": ["topic_id"],
            },
            handler=self._read_topic_detail,
            calling_convention="context",
        )


        self._register(
            name="read_session",
            description=(
                "Retrieve a past session record. "
                "full=false (default) returns date, summary, and topics discussed. "
                "full=true additionally returns the complete session transcript. "
                "Set full=true only when the user references a specific past exchange "
                "you need to recall verbatim. "
                "Obtain session_ids first via search_sessions_by_topic or "
                "search_sessions_by_summary."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID (timestamp string).",
                    },
                    "full": {
                        "type": "boolean",
                        "description": "Include full transcript. Default false.",
                    },
                },
                "required": ["session_id"],
            },
            handler=self._read_session,
            calling_convention="query",
        )


        self._register(
            name="search_sessions_by_topic",
            description=(
                "Semantic search over past sessions by their topics-discussed list. "
                "Use to find sessions that covered a particular concept "
                "(write a natural language description, not a topic_id). "
                "Returns a JSON list of session_ids sorted by relevance. "
                "Call read_session to retrieve details of any result."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Natural language description, "
                            "e.g. 'Python list comprehensions' or 'recursive functions'."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of session_ids to return (default 4, max 8).",
                    },
                },
                "required": ["query"],
            },
            handler=self._search_sessions_by_topic,
            calling_convention="query",
        )


        self._register(
            name="search_sessions_by_summary",
            description=(
                "Semantic search over past sessions by their session summary text. "
                "Use to find a session based on an event or observation that is "
                "not a named topic "
                "(e.g. 'session where the user got confused about scope' or "
                "'when we discussed their Unity project'). "
                "Returns a JSON list of session_ids sorted by relevance."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language description of what you are looking for.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of session_ids to return (default 4, max 8).",
                    },
                },
                "required": ["query"],
            },
            handler=self._search_sessions_by_summary,
            calling_convention="query",
        )

        self._register(
            name="search_topics",
            description=(
                "Semantic search over the topic knowledge base. "
                "Embeddings are based on each topic's title and short description. "
                "Use when: (1) checking whether a concept already exists in memory "
                "(a similarity score ≥ 0.9 strongly suggests it does); "
                "(2) finding related topics the user has already seen when planning "
                "teaching steps. "
                "Returns topic_ids with similarity scores and short descriptions. "
                "Call read_topic_detail for the full record of any result."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Natural language description of the concept, "
                            "e.g. 'mutable default arguments' or 'how recursion works'."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of topics to return (default 5, max 10).",
                    },
                },
                "required": ["query"],
            },
            handler=self._search_topics,
            calling_convention="context",
        )


        self._register(
            name="execute_code",
            description=(
                "Run a code snippet inside a Docker container and return stdout + stderr. "
                "Use when: demonstrating a runnable concept, or testing user-submitted "
                "code for grading/feedback. "
                "Returns JSON: {stdout, stderr, returncode}. "
                "Supported: python, javascript, cpp. "
                "For C++, pass complete self-contained source with all required headers. "
                "Do NOT call for Java, Rust, or any other language."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Code to execute.",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["python", "javascript", "cpp"],
                    },
                },
                "required": ["code", "language"],
            },
            handler=self._execute_code,
            calling_convention="query",
        )

        self._register(
            name="write_scratchpad",
            description=(
                "Record a mid-session observation to the session scratchpad. "
                "This is the ONLY mechanism for updating persistent memory at "
                "session end — if it is not in the scratchpad it will not be saved. "
                "Use informative keys:\n"
                "  - 'new_topic_<id>': JSON object for a new topic (full schema)\n"
                "  - 'user_level_<topic_id>': list of observed skill evidence for an existing topic\n"
                "  - 'note_<topic_id>': list of observations about the user's understanding of a specific topic\n"
                "  - 'preferences': list of evidence observed/stated for teaching preference\n"
                "  - 'goals': list of evidence for user goals\n"
                "  - 'demographic_<field>': list of volunteered personal information\n"
                "For all except new_topic records, the value is a natural language string that will be appended to a list of evidence regarding the key to accumulate evidence over the session. For new_topic entries, the value is a JSON object representing the new topic and should never be updated. In this case the tool has to be called with the 'append' parameter set to False."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Scratchpad key (see naming conventions above).",
                    },
                    "value": {
                        "type": "string",
                        "description": "Observation or updated value.",
                    },
                    "append": {
                        "type": "boolean",
                        "description": "Whether to append the value to an existing list.",
                    },
                },
                "required": ["key", "value"],
            },
            handler=self._write_scratchpad,
            calling_convention="context",
        )

        self._register(
            name="end_session",
            description=(
                "Signal that the session is over and trigger the wrap-up sequence. "
                "Call this only when the user has clearly and explicitly indicated "
                "they want to end the session (e.g. 'goodbye', 'that's all for today', "
                "'I have to go', 'let's stop here'). "
                "Do NOT call on a topic completing, a natural pause, or any ambiguous "
                "phrasing — if unsure, ask the user whether they want to continue. "
                "After calling this tool, deliver your wrap-up response normally."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=self._end_session,
            calling_convention="context",
        )

    def _read_topic_detail(self, topic_id: str) -> str:
        detail = self._memory.read_topic_detail(topic_id)
        if detail is None:
            return json.dumps({
                "result": None,
                "error":  f"Topic '{topic_id}' not found in memory.",
            })
        return json.dumps(detail, indent=2)

    def _read_session(self, session_id: str, full: bool = False) -> str:
        data = self._memory.read_session(session_id, full=full)
        if data is None:
            return f"Session '{session_id}' not found."
        return json.dumps(data, indent=2)

    def _search_sessions_by_topic(self, query: str, top_k: int = 4) -> str:
        top_k = min(max(1, top_k), 8)
        ids = self._memory.search_sessions_by_topic(query, top_k=top_k)
        return json.dumps(ids)

    def _search_sessions_by_summary(self, query: str, top_k: int = 4) -> str:
        top_k = min(max(1, top_k), 8)
        ids = self._memory.search_sessions_by_summary(query, top_k=top_k)
        return json.dumps(ids)

    def _search_topics(self, query: str, top_k: int = 5) -> str:
        top_k = min(max(1, top_k), 10)
        results = self._memory.search_topics(query, top_k=top_k)
        if not results:
            return json.dumps(["No matching topics found."])
        return json.dumps(results, indent=2)

    def _execute_code(self, code: str, language: str) -> str:
        if _DANGEROUS_PATTERNS.search(code):
            return json.dumps({
                "error":      "Execution blocked: unsafe pattern detected",
                "stdout":     "",
                "stderr":     "",
                "returncode": -1,
            })
        if language not in _DOCKER_LANGUAGES:
            return json.dumps({
                "error": (
                    f"Language '{language}' is not supported. "
                    f"Supported: {', '.join(_DOCKER_LANGUAGES)}."
                ),
                "stdout":     "",
                "stderr":     "",
                "returncode": -1,
            })
        if not _docker_available():
            return json.dumps({
                "error": (
                    "Docker is not available. "
                    "Ensure Docker Desktop (Windows/macOS) or Docker Engine (Linux) "
                    "is installed and running, then try again."
                ),
                "stdout":     "",
                "stderr":     "",
                "returncode": -1,
            })
        return json.dumps(_run_in_docker(language, code))

    def _write_scratchpad(self, key: str, value: str, append: bool = True) -> str:
        if append: 
            if key in self._scratchpad:
                if isinstance(self._scratchpad[key], list):
                    self._scratchpad[key].append(value)
                else:
                    self._scratchpad[key] = [self._scratchpad[key], value]
        else: 
            self._scratchpad[key] = value
        return f"Scratchpad updated: {key}"

    def _end_session(self) -> str:
        self._on_end_session()
        return "Session end acknowledged. Proceed with the wrap-up response."