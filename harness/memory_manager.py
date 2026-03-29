from __future__ import annotations

import json
import math
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


MEMORY_DIR   = Path("memory")
TOPICS_DIR   = MEMORY_DIR / "topics"
SESSIONS_DIR = MEMORY_DIR / "sessions"

TOPIC_INDEX_PATH                = MEMORY_DIR / "topic_index.json"
TOPIC_EMBEDDINGS_PATH           = MEMORY_DIR / "topic_embeddings.json"

SESSION_INDEX_PATH              = MEMORY_DIR / "session_index.json"
SESSION_SUMMARY_EMBEDDINGS_PATH = MEMORY_DIR / "session_summary_embeddings.json"
SESSION_TOPICS_EMBEDDINGS_PATH  = MEMORY_DIR / "session_topics_embeddings.json"

USER_PROFILE_PATH       = MEMORY_DIR / "user_profile.json"
SELF_ASSESSMENT_PATH    = MEMORY_DIR / "agent_self_assessment.txt"
PENDING_UPDATES_PATH    = MEMORY_DIR / "pending_updates.json"


def _ensure_dirs():
    for d in [MEMORY_DIR, TOPICS_DIR, SESSIONS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


_embed_model = None


def _get_embed_model():
    global _embed_model
    if _embed_model is not None:
        return _embed_model
    try:
        import logging
        from sentence_transformers import SentenceTransformer
        cache_dir = Path("cache/model_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        model_path = cache_dir / "all-MiniLM-L6-v2"

        _noisy = ["transformers", "sentence_transformers"]
        _saved = {}
        for n in _noisy:
            lg = logging.getLogger(n)
            _saved[n] = lg.level
            lg.setLevel(logging.ERROR)
        try:
            if model_path.exists():
                _embed_model = SentenceTransformer(str(model_path))
            else:
                print("[SemanticIndex] Downloading embedding model (one-time, ~23 MB)...")
                _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
                _embed_model.save(str(model_path))
                print(f"[SemanticIndex] Model saved to {model_path}")
        finally:
            for n, lv in _saved.items():
                logging.getLogger(n).setLevel(lv)
        return _embed_model
    except ImportError:
        return None


def _embed(text: str) -> list[float] | None:
    model = _get_embed_model()
    if model is None:
        return None
    vec = model.encode(text, convert_to_numpy=True)
    return vec.tolist()


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na * nb > 0 else 0.0


def _load_embedding_file(path: Path) -> dict[str, list[float]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_embedding_file(path: Path, data: dict[str, list[float]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _add_embedding_if_absent(path: Path, entry_id: str, text: str) -> bool:
    """Embed text and store under entry_id — write-once. Returns True if added."""
    data = _load_embedding_file(path)
    if entry_id in data:
        return False
    vec = _embed(text)
    if vec is None:
        return False
    data[entry_id] = vec
    _save_embedding_file(path, data)
    return True


def _semantic_search(
    emb_path: Path,
    query: str,
    top_k: int,
    threshold: float = 0.25,
) -> list[tuple[str, float]]:
    """Returns [(id, score)] sorted by descending score, or [] on failure."""
    q_vec = _embed(query)
    if q_vec is None:
        return []
    data = _load_embedding_file(emb_path)
    scored = [
        (eid, _cosine(q_vec, vec))
        for eid, vec in data.items()
    ]
    scored = [(eid, s) for eid, s in scored if s >= threshold]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]



def _read_json(path: Path, default=None):
    if default is None:
        default = {}
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, data: Any) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, Path(str(path) + ".bak"))
    try:
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return True
    except (OSError, TypeError) as e:
        print(f"[Memory] Write failed for {path}: {e}")
        return False


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_text(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, Path(str(path) + ".bak"))
    try:
        path.write_text(content, encoding="utf-8")
        return True
    except OSError as e:
        print(f"[Memory] Write failed for {path}: {e}")
        return False



class MemoryManager:
    """
    Central gateway for all memory file I/O.
    The harness calls this; the model never writes files directly.
    """

    def __init__(self):
        _ensure_dirs()


    def is_initialized(self) -> bool:
        return USER_PROFILE_PATH.exists()

    def has_pending_updates(self) -> bool:
        return PENDING_UPDATES_PATH.exists()


    def load_auto_context(self) -> dict:
        """
        Returns a dict with all fields needed to build the system-prompt
        memory block.

        Deliberately does NOT load the full topic index — agents use
        search_topics instead (per spec).

        Only the 3 most recent session summaries are included.
        """
        profile     = _read_json(USER_PROFILE_PATH, {})
        assessment  = _read_text(SELF_ASSESSMENT_PATH)

        # 3 most recent sessions (summaries only, no transcripts)
        session_index = _read_json(SESSION_INDEX_PATH, [])
        recent_sessions = session_index[-3:][::-1]  # newest first

        return {
            "user_profile":          profile,
            "agent_self_assessment": assessment,
            "recent_sessions":       recent_sessions,
        }


    def read_topic_index(self) -> dict:
        """Returns {topic_id: {title, short_description}}."""
        return _read_json(TOPIC_INDEX_PATH, {})

    def topic_exists(self, topic_id: str) -> bool:
        return (TOPICS_DIR / f"{topic_id}.json").exists()


    def read_topic_detail(self, topic_id: str) -> dict | None:
        """
        Returns the full topic record (all fields including immutable ones).
        Returns None if not found.
        """
        path = TOPICS_DIR / f"{topic_id}.json"
        if not path.exists():
            return None
        return _read_json(path, None)


    def create_topic(self, topic: dict) -> bool:
        """
        Create a brand-new topic from a complete topic dict.
        The dict must contain all fields including immutable ones
        (id, title, short_description).

        Returns False if the topic_id already exists (idempotent guard).
        Adds the topic to topic_index.json and topic_embeddings.json.
        """
        topic_id = topic.get("id", "")
        if not topic_id:
            print("[Memory] create_topic: missing 'id' field.")
            return False
        if self.topic_exists(topic_id):
            return False  # already exists

        # Ensure required fields are present
        record = {
            "id":                  topic_id,
            "title":               topic.get("title", topic_id),
            "short_description":   topic.get("short_description", ""),
            "user_level":          topic.get("user_level", "unknown"),
            "note":                topic.get("note", ""),
        }

        if not _write_json(TOPICS_DIR / f"{topic_id}.json", record):
            return False

        # Update topic index
        index = _read_json(TOPIC_INDEX_PATH, {})
        index[topic_id] = {
            "title":             record["title"],
            "short_description": record["short_description"],
        }
        _write_json(TOPIC_INDEX_PATH, index)

        # Write-once embedding (title + short_description)
        embed_text = f"{record['title']}: {record['short_description']}"
        _add_embedding_if_absent(TOPIC_EMBEDDINGS_PATH, topic_id, embed_text)

        return True

    def update_topic_mutable_fields(self, topic_id: str, updates: dict) -> bool:
        """
        Update mutable fields of an existing topic.
        Immutable fields (id, title, short_description) are
        silently protected — any values in `updates` for those keys are ignored.
        """
        path = TOPICS_DIR / f"{topic_id}.json"
        if not path.exists():
            return False
        data = _read_json(path, {})
        immutable = {"id", "title", "short_description"}
        for k, v in updates.items():
            if k not in immutable:
                data[k] = v
        return _write_json(path, data)


    def search_topics(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Returns list of {topic_id, score, short_description} sorted by score.
        Falls back to keyword search if embeddings unavailable.
        """
        results = _semantic_search(TOPIC_EMBEDDINGS_PATH, query, top_k)
        if results:
            index = _read_json(TOPIC_INDEX_PATH, {})
            out = []
            for tid, score in results:
                entry = index.get(tid, {})
                out.append({
                    "topic_id":          tid,
                    "score":             round(score, 3),
                    "short_description": entry.get("short_description", ""),
                })
            return out
        return self._keyword_search_topics(query, top_k)


    def read_session_index(self) -> list[dict]:
        """Returns full list of session index entries."""
        return _read_json(SESSION_INDEX_PATH, [])

    def read_session(self, session_id: str, full: bool = False) -> dict | None:
        """
        full=False: {session_id, summary, topics}
        full=True:  adds 'transcript'
        """
        path = SESSIONS_DIR / f"{session_id}.json"
        if not path.exists():
            return None
        data = _read_json(path, None)
        if data is None:
            return None
        if not full:
            return {k: v for k, v in data.items() if k != "transcript"}
        return data


    def write_session(
        self,
        session_id: str,
        summary: str,
        topics: list[dict],    # [{id, title}] — validated against topic index
        transcript: str,

    ) -> bool:
        """
        Write session record + update session index + update embeddings.
        topics entries whose id is not in the topic index are dropped with
        a warning (prevents dangling references).
        """
        topic_index = _read_json(TOPIC_INDEX_PATH, {})
        validated_topics = []
        for t in topics:
            tid = t.get("id", "")
            if tid in topic_index:
                validated_topics.append({
                    "id":    tid,
                    "title": topic_index[tid]["title"],
                })
            else:
                print(f"[Memory] Session topic '{tid}' not in topic index — skipped.")

        record = {
            "session_id": session_id,
            "summary":    summary,
            "topics":     validated_topics,
            "transcript": transcript,
        }
        if not _write_json(SESSIONS_DIR / f"{session_id}.json", record):
            return False

        # Update session index (root-level file)
        idx = _read_json(SESSION_INDEX_PATH, [])
        idx = [e for e in idx if e.get("session_id") != session_id]
        idx.append({
            "session_id": session_id,
            "summary":    summary,
            "topics":     validated_topics,
        })
        idx.sort(key=lambda e: e["session_id"])  # keep chronological
        _write_json(SESSION_INDEX_PATH, idx)

        # Write-once embeddings (root-level files)
        _add_embedding_if_absent(
            SESSION_SUMMARY_EMBEDDINGS_PATH, session_id, summary
        )
        topics_text = " ".join(t["title"] for t in validated_topics)
        if topics_text:
            _add_embedding_if_absent(
                SESSION_TOPICS_EMBEDDINGS_PATH, session_id, topics_text
            )

        return True


    def search_sessions_by_topic(self, query: str, top_k: int = 4) -> list[str]:
        """Returns session_ids sorted by topic-list relevance."""
        results = _semantic_search(SESSION_TOPICS_EMBEDDINGS_PATH, query, top_k)
        if results:
            return [sid for sid, _ in results]
        return self._keyword_search_sessions(query, top_k)

    def search_sessions_by_summary(self, query: str, top_k: int = 4) -> list[str]:
        """Returns session_ids sorted by summary relevance."""
        results = _semantic_search(SESSION_SUMMARY_EMBEDDINGS_PATH, query, top_k)
        if results:
            return [sid for sid, _ in results]
        return self._keyword_search_sessions(query, top_k)


    def read_user_profile(self) -> dict:
        return _read_json(USER_PROFILE_PATH, {})

    def write_user_profile(self, profile: dict) -> bool:
        return _write_json(USER_PROFILE_PATH, profile)


    def read_self_assessment(self) -> str:
        return _read_text(SELF_ASSESSMENT_PATH)

    def write_self_assessment(self, content: str) -> bool:
        return _write_text(SELF_ASSESSMENT_PATH, content)


    def load_pending_updates(self) -> dict | None:
        if not PENDING_UPDATES_PATH.exists():
            return None
        return _read_json(PENDING_UPDATES_PATH, None)

    def save_pending_updates(self, pending: dict):
        _write_json(PENDING_UPDATES_PATH, pending)

    def clear_pending_updates(self):
        if PENDING_UPDATES_PATH.exists():
            PENDING_UPDATES_PATH.unlink()

    def increment_pending_retry(self):
        pending = self.load_pending_updates()
        if pending:
            pending["retry_count"] = pending.get("retry_count", 0) + 1
            self.save_pending_updates(pending)


    def initialize_empty_memory(self):
        """
        Write default empty files for a fresh installation.
        Topic index and embedding files are NOT created here — they come into
        existence naturally the first time a topic is created mid-session.
        """
        _write_json(USER_PROFILE_PATH, {
            "demographic_info":          {},
            "general_coder_level":       "unknown",
            "language_skill_levels":     {},
            "user_preferences":          "",
            "user_goals":                "",
        })
        _write_text(SELF_ASSESSMENT_PATH, "First session — no prior observations.")
        _write_json(SESSION_INDEX_PATH, [])



    def _keyword_search_sessions(self, query: str, top_k: int) -> list[str]:
        idx = _read_json(SESSION_INDEX_PATH, [])
        q   = query.lower()
        matches = [
            e["session_id"] for e in idx
            if q in e.get("summary", "").lower()
            or any(q in t.get("title", "").lower() for t in e.get("topics", []))
        ]
        return matches[:top_k]

    def _keyword_search_topics(self, query: str, top_k: int) -> list[dict]:
        index = _read_json(TOPIC_INDEX_PATH, {})
        q     = query.lower()
        results = []
        for tid, entry in index.items():
            text = (entry.get("title", "") + " " +
                    entry.get("short_description", "")).lower()
            if q in text:
                results.append({
                    "topic_id":          tid,
                    "score":             0.5,
                    "short_description": entry.get("short_description", ""),
                })
        return results[:top_k]
