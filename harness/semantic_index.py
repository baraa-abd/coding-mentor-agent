from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # avoid circular imports



MEMORY_DIR = Path("memory")
SESSION_EMBED_PATH = MEMORY_DIR / "session_embeddings.json"
TOPIC_EMBED_PATH   = MEMORY_DIR / "topic_embeddings.json"


MODEL_CACHE_DIR = "cache/model_cache"


DEFAULT_THRESHOLD = 0.30

DEFAULT_TOP_K = 4


EMBED_MODEL = "all-MiniLM-L6-v2"


_model = None  # module-level singleton


def _get_model():
    """
    Load (or return cached) sentence-transformers model.

    On first call the model is downloaded from HuggingFace and saved to
    MODEL_CACHE_DIR.  Every subsequent call (including across process
    restarts) loads directly from disk — no network required.

    The transformers library logs an informational "LOAD REPORT" about
    unexpected checkpoint keys (e.g. embeddings.position_ids) that is
    harmless but noisy.  It is suppressed here by raising the log level
    of the relevant loggers to ERROR for the duration of the load, then
    restoring the original level.
    """
    global _model
    if _model is not None:
        return _model

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        raise RuntimeError(
            "sentence-transformers is required for semantic search. "
            "Install it with: pip install sentence-transformers"
        )

    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    model_path = MODEL_CACHE_DIR / EMBED_MODEL

    # Suppress the noisy LOAD REPORT emitted by the transformers library
    # when it encounters checkpoint keys it does not use.  These are harmless
    # (the report itself says "can be ignored") but clutter the terminal.
    # We restore the original level immediately after loading.
    _noisy_loggers = ["transformers", "sentence_transformers"]
    _saved_levels = {}
    for name in _noisy_loggers:
        logger = logging.getLogger(name)
        _saved_levels[name] = logger.level
        logger.setLevel(logging.ERROR)

    try:
        if model_path.exists():
            # Load from local disk — no network call, no download progress bar.
            _model = SentenceTransformer(str(model_path))
        else:
            # First run: download and immediately save to MODEL_CACHE_DIR so
            # future startups skip the network entirely.
            print(f"[SemanticIndex] Downloading embedding model '{EMBED_MODEL}' "
                  f"(one-time, ~23 MB)...")
            _model = SentenceTransformer(EMBED_MODEL)
            _model.save(str(model_path))
            print(f"[SemanticIndex] Model saved to {model_path} — "
                  "future startups will load from disk.")
    finally:
        # Always restore log levels, even if loading raised an exception.
        for name, level in _saved_levels.items():
            logging.getLogger(name).setLevel(level)

    return _model


# ---------------------------------------------------------------------------
# Pure math helpers (no numpy dependency at import time)
# ---------------------------------------------------------------------------

def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _norm(a: list[float]) -> float:
    return math.sqrt(sum(x * x for x in a))


def _cosine(a: list[float], b: list[float]) -> float:
    denom = _norm(a) * _norm(b)
    if denom == 0.0:
        return 0.0
    return _dot(a, b) / denom


def _embed(text: str) -> list[float]:
    """Embed a single string; return as plain Python list."""
    model = _get_model()
    vec = model.encode(text, convert_to_numpy=True)
    return vec.tolist()


# ---------------------------------------------------------------------------
# SemanticIndex
# ---------------------------------------------------------------------------

class SemanticIndex:
    """
    Manages a single embedding index stored at `index_path`.

    Thread-safety: single-threaded use only (matches the rest of the harness).
    """

    def __init__(self, index_path: Path):
        self._path = index_path
        self._data: dict[str, dict] | None = None  # lazy load

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def add_if_absent(self, entry_id: str, text: str) -> bool:
        """
        Embed `text` and store under `entry_id` — only if not already present.
        Returns True if a new entry was added, False if it already existed.

        This is the write-once guarantee: existing entries are never touched.
        """
        data = self._load()
        if entry_id in data:
            return False  # Already indexed — do not overwrite

        try:
            embedding = _embed(text)
        except Exception as exc:
            print(f"[SemanticIndex] Embedding failed for '{entry_id}': {exc}")
            return False

        data[entry_id] = {"text": text, "embedding": embedding}
        self._save(data)
        return True

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> list[dict]:
        """
        Return up to `top_k` entries whose similarity to `query` exceeds
        `threshold`, sorted by descending similarity.

        Each result dict:
            {"id": str, "text": str, "score": float}
        """
        data = self._load()
        if not data:
            return []

        try:
            q_vec = _embed(query)
        except Exception as exc:
            print(f"[SemanticIndex] Query embedding failed: {exc}")
            return []

        scored = []
        for entry_id, entry in data.items():
            emb = entry.get("embedding")
            if not emb:
                continue
            score = _cosine(q_vec, emb)
            if score >= threshold:
                scored.append({"id": entry_id, "text": entry["text"], "score": score})

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    # ------------------------------------------------------------------
    # Bulk re-index helper (for migrating existing memory directories)
    # ------------------------------------------------------------------

    def index_missing(self, entries: dict[str, str]) -> int:
        """
        Given a {id: text} dict, index any entries not already present.
        Returns the number of newly added entries.
        """
        added = 0
        for entry_id, text in entries.items():
            if self.add_if_absent(entry_id, text):
                added += 1
        return added

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        """Load index from disk into memory cache."""
        if self._data is not None:
            return self._data
        if not self._path.exists():
            self._data = {}
            return self._data
        try:
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._data = {}
        return self._data

    def _save(self, data: dict):
        """Persist index to disk and update in-memory cache."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data), encoding="utf-8")
        self._data = data


# ---------------------------------------------------------------------------
# Module-level convenience instances (shared across MemoryManager calls)
# ---------------------------------------------------------------------------
session_index = SemanticIndex(SESSION_EMBED_PATH)
topic_index   = SemanticIndex(TOPIC_EMBED_PATH)