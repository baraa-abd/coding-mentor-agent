import json
import re
from datetime import datetime
from enum import Enum, auto

from harness.memory_manager import MemoryManager
from harness.tool_registry import ToolRegistry
from harness.prompts import *
from llm import OllamaClient

# Number of recent transcript entries kept verbatim in the messages array
VERBATIM_WINDOW = 15



class SessionState(Enum):
    ACTIVE  = auto()   # Normal conversation; full tool access
    WRAP_UP = auto()   # End-of-session flow; limited interaction
    DONE    = auto()   # Terminal; triggers update sequence then exit


class MentorHarness:
    def __init__(self, llm: OllamaClient):
        self._llm        = llm
        self._memory     = MemoryManager()
        self._session_id = datetime.now().strftime("%Y_%m_%dT%H_%M_%S")
        self._state      = SessionState.ACTIVE
        self._scratchpad: dict = {}

        self._transcript: list[list[dict]] = []
        self._rolling_summary: str = ""
        self._turns_since_summary: int = 0 

        if not self._memory.is_initialized():
            print("First run detected — initializing memory...")
            self._memory.initialize_empty_memory()
            print("  ✓ Memory initialized.")
        elif self._memory.has_pending_updates():
            self._recover_pending_updates()

        # Auto-loaded memory block — built once at session start from disk
        auto_context     = self._memory.load_auto_context()
        self._auto_block = _build_auto_memory_block(auto_context)

        self._tools = ToolRegistry(
            self._memory, self._scratchpad,
            on_end_session=self._handle_end_session,
        )

    # Main step
    def step(self, user_input: str) -> str:
        if self._state == SessionState.DONE:
            return "[Session ended]"

        self._transcript.append([{"role": "user", "content": user_input}])

        system   = self._build_system_prompt()
        messages = [msg for entry in self._transcript[-VERBATIM_WINDOW:] for msg in entry]

        final_response = self._agentic_loop(system, messages)

        if final_response:
            self._transcript.append([{"role": "assistant", "content": final_response}])

        self._turns_since_summary += 1
        self._maybe_update_summary()

        if self._state == SessionState.WRAP_UP:
            self._state = SessionState.DONE

        return final_response

    # Agentic loop
    def _agentic_loop(self, system: str, messages: list[dict]) -> str:
        while True:
            active_system = system if self._state != SessionState.WRAP_UP else system+WRAPUP_SYSTEM_PROMPT_FRAGMENT

            response = self._llm.complete(
                messages=messages,
                system=active_system,
                tools=self._tools.schemas(),
                stream=True,
            )
            if response.has_tool_calls:
                results       = self._tools.dispatch(response.tool_calls)
                exchange_msgs = format_tool_exchange(
                    response.text, response.tool_calls, results
                )
                self._transcript.append(exchange_msgs)
                messages.extend(exchange_msgs)

                for tc, tr in zip(response.tool_calls, results):
                    if tc.name == "execute_code":
                        lang = tc.arguments.get("language", "")
                        code = tc.arguments.get("code", "")
                        print(f"\n```{lang}\n{code}\n```")
                        print(f"\n**Output:**\n{tr.content}\n")
                    if tc.name == "search_topics":
                        print(f"\n**Topic search results:**\n{tr.content}\n")
            else:
                return response.text

    @property
    def done(self) -> bool:
        return self._state == SessionState.DONE

    def greet(self) -> str:
        """
        Generate and record the opening greeting for this session.
        Streams the greeting to stdout and records it as an assistant turn.
        """
        is_first = not self._memory.read_session_index()
        profile  = self._memory.read_user_profile()

        response = self._llm.complete(
            messages=[{"role": "user", "content": GREETING_PROMPT(is_first, profile)}],
            system=self._build_system_prompt(),
            tools=None,
            stream=True,
        )
        if response.text:
            self._transcript.append([{"role": "assistant", "content": response.text}])
        return response.text

    def _handle_end_session(self):
        """Callback invoked by the end_session tool to transition state."""
        self._state = SessionState.WRAP_UP

    def run_end_of_session_updates(self):
        """
        Execute all memory updates after the session ends.
        All updates are driven by the scratchpad (+ transcript for summary).
        On partial failure, session_summary and scratchpad are saved for
        recovery at the next session start.
        """
        print("\n[Memory] Running end-of-session updates...")
        transcript      = _render_transcript(self._transcript)
        session_summary = self._run_session_summary(transcript)

        pending = {
            "session_id":      self._session_id,
            "session_summary": session_summary,
            "scratchpad":      self._scratchpad,
            "pending_steps":   [],
            "retry_count":     0,
            "max_retries":     3,
        }

        steps = [
            ("topic updates",         self._update_topics),
            ("session record",        lambda: self._write_session_record(session_summary, transcript)),
            ("user profile",          lambda: self._update_user_profile(session_summary)),
            ("agent self-assessment", lambda: self._update_self_assessment(session_summary)),
        ]

        for label, fn in steps:
            try:
                fn()
                print(f"  ✓ {label}")
            except Exception as e:
                print(f"  ✗ {label}: {e}")
                pending["pending_steps"].append({"step": label, "error": str(e)})

        if pending["pending_steps"]:
            self._memory.save_pending_updates(pending)
            print("[Memory] Some updates deferred — will retry next session.")
        else:
            self._memory.clear_pending_updates()
            print("[Memory] All updates complete.")



    def _build_system_prompt(self) -> str:
        """Assemble the full system prompt for the current turn."""
        parts = [BASE_SYSTEM_PROMPT]

        if self._auto_block:
            parts.append("\n\n[AUTO-LOADED MEMORY]\n" + self._auto_block)

        if self._scratchpad:
            parts.append("\n\n" + self._scratchpad_text())

        if self._rolling_summary:
            parts.append(f"\n\n[Session Summary So Far]\n{self._rolling_summary}")

        return "".join(parts)


    def _scratchpad_text(self):
        """Rebuild the scratchpad system-prompt block from the current scratchpad."""
        if not self._scratchpad:
            return ""
        lines = [f"  {k}: {v}" for k, v in self._scratchpad.items()]
        return ("[Session Scratchpad - more current than memory files]\n"+"\n".join(lines))

    def _maybe_update_summary(self):
        """
        Update the rolling summary every VERBATIM_WINDOW turns.
        Summarises the oldest VERBATIM_WINDOW entries and merges them
        into the existing summary.
        """
        if self._turns_since_summary < VERBATIM_WINDOW:
            return
        self._turns_since_summary = 0

        entries_to_summarise = self._transcript[:VERBATIM_WINDOW]
        recent_text = _render_transcript(entries_to_summarise)

        try:
            response = self._llm.complete(
                messages=[{"role": "user",
                           "content": ROLLING_SUMMARY_PROMPT(
                               self._rolling_summary, recent_text
                           )}],
                system=ROLLING_SUMMARY_SYSTEM_PROMPT,
                stream=False,
            )
            if response.text:
                self._rolling_summary = response.text.strip()
        except Exception as e:
            print(f"[Summary] Update failed (non-fatal): {e}")


    def _recover_pending_updates(self):
        """Re-attempt deferred memory updates from a previous crashed session."""
        pending = self._memory.load_pending_updates()
        if not pending:
            return
        if pending.get("retry_count", 0) >= pending.get("max_retries", 3):
            print("[Memory] Max retries exceeded — clearing pending updates.")
            self._memory.clear_pending_updates()
            return

        print(f"[Memory] Recovering {len(pending['pending_steps'])} deferred update(s)...")
        self._memory.increment_pending_retry()

        context = json.dumps({
            "session_summary": pending.get("session_summary", ""),
            "scratchpad":      pending.get("scratchpad", {}),
        }, indent=2)

        response = self._llm.complete(
            messages=[{"role": "user",
                       "content": MEMORY_REATTEMPT_PROMPT(
                           json.dumps(pending["pending_steps"], indent=2),
                           context,
                       )}],
            system=MEMORY_REATTEMPT_SYSTEM_PROMPT,
            stream=False,
        )
        raw = _strip_fences(response.text)
        try:
            updates: dict = json.loads(raw)
            for filename, content in updates.items():
                if filename == "user_profile.json":
                    self._memory.write_user_profile(content)
                elif filename == "agent_self_assessment.txt":
                    self._memory.write_self_assessment(content)
            self._memory.clear_pending_updates()
            print("  ✓ Pending updates recovered.")
        except Exception as e:
            print(f"  ✗ Recovery failed: {e}")


    # End-of-session helpers

    def _run_session_summary(self, transcript: str) -> str:
        response = self._llm.complete(
            messages=[{"role": "user",
                       "content": SESSION_SUMMARY_PROMPT(transcript[:5000])}],
            system=SESSION_SUMMARY_SYSTEM_PROMPT,
            stream=False,
        )
        return response.text.strip()

    def _extract_discussed_topics(self) -> list[str]:
        if not self._scratchpad:
            return []
        response = self._llm.complete(
            messages=[{"role": "user",
                       "content": EXTRACT_DISCUSSED_TOPICS_PROMPT(self._scratchpad)}],
            system=EXTRACT_DISCUSSED_TOPICS_SYSTEM_PROMPT,
            stream=False,
        )
        raw = _strip_fences(response.text)
        try:
            ids = json.loads(raw)
            if isinstance(ids, list):
                return [str(i) for i in ids]
        except (json.JSONDecodeError, TypeError):
            pass
        return []

    def _write_session_record(self, session_summary: str, transcript: str):
        topic_ids   = self._extract_discussed_topics()
        topic_index = self._memory.read_topic_index()
        topics = [
            {"id": tid, "title": topic_index[tid]["title"]}
            if tid in topic_index else {"id": tid, "title": tid}
            for tid in topic_ids
        ]
        self._memory.write_session(
            session_id=self._session_id,
            summary=session_summary,
            topics=topics,
            transcript=transcript,
        )

    def _update_topics(self):
        if not self._scratchpad:
            return
        response = self._llm.complete(
            messages=[{"role": "user",
                       "content": UPDATE_TOPICS_PROMPT(
                           self._scratchpad, self._session_id
                       )}],
            system=UPDATE_TOPICS_SYSTEM_PROMPT,
            stream=False,
        )
        topic_updates: dict = json.loads(_strip_fences(response.text))

        for tid, fields in topic_updates.items():
            if self._memory.topic_exists(tid):
                self._memory.update_topic_mutable_fields(tid, fields)
            else:
                fields["id"] = tid
                if not self._memory.create_topic(fields):
                    print(f"  [Memory] Failed to create topic '{tid}'")

    def _update_user_profile(self, session_summary: str):
        current = self._memory.read_user_profile()
        response = self._llm.complete(
            messages=[{"role": "user",
                       "content": UPDATE_USER_PROFILE_PROMPT(
                           json.dumps(current, indent=2),
                           session_summary,
                           self._scratchpad,
                           self._session_id,
                       )}],
            system=UPDATE_USER_PROFILE_SYSTEM_PROMPT,
            stream=False,
        )
        self._memory.write_user_profile(json.loads(_strip_fences(response.text)))

    def _update_self_assessment(self, session_summary: str):
        current = self._memory.read_self_assessment()
        response = self._llm.complete(
            messages=[{"role": "user",
                       "content": UPDATE_SELF_ASSESSMENT_PROMPT(
                           session_summary,
                           self._scratchpad,
                           current,
                       )}],
            system=UPDATE_SELF_ASSESSMENT_SYSTEM_PROMPT,
            stream=False,
        )
        self._memory.write_self_assessment(response.text.strip())



def _render_transcript(entries: list[list[dict]]) -> str:
    parts = []
    for entry in entries:
        for msg in entry:
            role = msg.get("role", "").upper()
            if role == "ASSISTANT":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    calls = [
                        f"{tc['function']['name']}({tc['function']['arguments']})"
                        for tc in tool_calls
                    ]
                    parts.append(f"ASSISTANT (tool calls): {', '.join(calls)}")
                elif msg.get("content"):
                    parts.append(f"ASSISTANT: {msg['content']}")
            elif role == "TOOL":
                parts.append(
                    f"TOOL RESULT ({msg.get('tool_call_id', '')}): "
                    f"{msg.get('content', '')}"
                )
            else:
                parts.append(f"{role}: {msg.get('content', '')}")
    return "\n".join(parts)


def _build_auto_memory_block(context: dict) -> str:
    parts = []

    profile = context.get("user_profile", {})
    if profile:
        parts.append("### USER PROFILE\n" + json.dumps(profile, indent=2))

    assessment = context.get("agent_self_assessment", "").strip()
    if assessment:
        parts.append("### AGENT SELF-ASSESSMENT (private)\n" + assessment)

    recent = context.get("recent_sessions", [])
    if recent:
        lines = []
        for s in recent:
            topics_str = ", ".join(
                t.get("title", t.get("id", "?")) for t in s.get("topics", [])
            )
            lines.append(
                f"- {s['session_id']} ({s.get('date', '')}): "
                f"{s.get('summary', '')} | Topics: {topics_str}"
            )
        parts.append("### RECENT SESSIONS (last 3)\n" + "\n".join(lines))

    return "\n\n".join(parts)


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()

def format_tool_exchange(
    response_text: str,
    tool_calls: list[ToolCall],
    results: list[ToolResult],
) -> list[dict]:
    assistant_msg: dict = {
        "role": "assistant",
        "content": response_text or None,
        "tool_calls": [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                },
            }
            for tc in tool_calls
        ],
    }

    result_msgs = [
        {
            "role": "tool",
            "tool_call_id": r.tool_call_id,
            "content": r.content,
        }
        for r in results
    ]
    return [assistant_msg] + result_msgs
