"""Hermes Secret Context v2.1 — Memory Wiki metadata-only context bridge."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

VERSION = "2.1.0"


def _load_core():
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    candidates = [
        Path(os.environ.get("HERMES_SECRET_CORE_PATH", "")).expanduser() if os.environ.get("HERMES_SECRET_CORE_PATH") else None,
        home / "lib",
        Path(__file__).resolve().parent.parent.parent / "lib",
    ]
    for candidate in candidates:
        if candidate and candidate.exists() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    from hermes_secret_core import MemorySecretIndex, inject_context, render_secret_context
    return MemorySecretIndex, inject_context, render_secret_context


def _index():
    MemorySecretIndex, _, _ = _load_core()
    return MemorySecretIndex()


def secret_context_lookup(secret_id: str, reveal: bool = False, allow_sensitive: bool = False) -> dict[str, Any]:
    if reveal or allow_sensitive:
        return {
            "error": "plaintext_reveal_disabled",
            "detail": "Pass the sec_* identifier to an authorized executor; capability and plaintext remain internal to that process.",
        }
    try:
        row = _index().get(str(secret_id or ""))
        return row or {"error": "secret_not_found", "secret_id": secret_id}
    except FileNotFoundError:
        return {"error": "memory_wiki_database_not_found"}
    except Exception as exc:
        return {"error": "secret_context_lookup_failed", "detail": str(exc)[:300]}


def secret_context_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    try:
        return _index().search(query, limit)
    except FileNotFoundError:
        return []
    except Exception:
        return []


def secret_context_list_aliases(limit: int = 200) -> list[dict[str, Any]]:
    try:
        return _index().list_aliases(limit)
    except Exception:
        return []


def pre_llm_call(messages: list[dict[str, Any]], **kwargs):
    user_text = ""
    for message in reversed(messages or []):
        if message.get("role") == "user":
            content = message.get("content", "")
            user_text = content if isinstance(content, str) else str(content)
            break
    if not user_text.strip():
        return [dict(m) for m in (messages or [])]
    matches = secret_context_search(user_text, 5)
    _, inject_context, render_secret_context = _load_core()
    return inject_context(messages or [], render_secret_context(matches))


def register(ctx):
    ctx.register_tool(
        name="secret_context_lookup",
        toolset="secret-context",
        schema={
            "type": "object",
            "properties": {"secret_id": {"type": "string", "description": "Memory Wiki sec_* identifier"}},
            "required": ["secret_id"],
            "additionalProperties": False,
        },
        handler=lambda secret_id, **kw: secret_context_lookup(secret_id),
    )
    ctx.register_tool(
        name="secret_context_search",
        toolset="secret-context",
        schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10}},
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=lambda query, limit=5, **kw: {"matches": secret_context_search(query, limit)},
    )
    ctx.register_hook("pre_llm_call", pre_llm_call)
