"""Hermes Secret Context v2.2 — Memory Wiki metadata-only context bridge."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

VERSION = "2.2.0"


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
    from hermes_secret_core import MemorySecretIndex, inject_context, render_secret_context, require_version
    require_version((2, 2, 0))
    return MemorySecretIndex, inject_context, render_secret_context


def _index():
    MemorySecretIndex, _, _ = _load_core()
    return MemorySecretIndex()


def secret_context_lookup(secret_id: str, **_ignored: Any) -> dict[str, Any]:
    try:
        row = _index().get(str(secret_id or ""))
        return row or {"error": "secret_not_found", "secret_id": secret_id}
    except FileNotFoundError:
        return {"error": "memory_wiki_database_not_found"}
    except Exception:
        return {"error": "secret_context_lookup_failed"}


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


def _content_text(content: Any, *, limit: int = 6000) -> str:
    """Extract only textual user content without stringifying binary/tool objects."""
    chunks: list[str] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 4 or sum(len(x) for x in chunks) >= limit:
            return
        if isinstance(value, str):
            chunks.append(value[: max(0, limit - sum(len(x) for x in chunks))])
        elif isinstance(value, dict):
            kind = str(value.get("type") or "").casefold()
            if kind in {"text", "input_text", "output_text"}:
                visit(value.get("text", ""), depth + 1)
            elif "content" in value:
                visit(value.get("content"), depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value[:32]:
                visit(item, depth + 1)

    visit(content)
    return "\n".join(chunks)[:limit]

def pre_llm_call(messages: list[dict[str, Any]], **kwargs):
    user_text = ""
    for message in reversed(messages or []):
        if message.get("role") == "user":
            user_text = _content_text(message.get("content", ""))
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
