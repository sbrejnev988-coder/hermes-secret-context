"""
secret-context — Hermes plugin for vault-based secret injection.
Reads secrets_registry.json on pre_llm_call and injects
redacted secret context into the system prompt.
"""

import json
import os
from pathlib import Path

VERSION = "1.0.0"
_VAULT_CACHE = {}
_VAULT_CACHE_MTIME = 0


def _load_vault():
    """Load secrets_registry.json with caching."""
    global _VAULT_CACHE, _VAULT_CACHE_MTIME
    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    vault_path = Path(hermes_home) / "vault" / "secrets_registry.json"
    if not vault_path.exists():
        return {"secrets": []}
    mtime = vault_path.stat().st_mtime
    if mtime == _VAULT_CACHE_MTIME:
        return _VAULT_CACHE
    with open(vault_path) as f:
        vault = json.load(f)
    _VAULT_CACHE = vault
    _VAULT_CACHE_MTIME = mtime
    return vault


def secret_context_lookup(secret_id, reveal=False, allow_sensitive=False):
    """
    Look up a secret from the vault.
    Args:
        secret_id: The secret ID to look up
        reveal: If True, return full secret values. Default: redacted.
        allow_sensitive: Must be True for reveal to work.
    Returns:
        dict with secret data (redacted unless reveal=True + allow_sensitive=True)
    """
    vault = _load_vault()
    for s in vault.get("secrets", []):
        if s.get("id") == secret_id:
            result = {
                "id": s.get("id"),
                "host": s.get("host"),
                "login": s.get("login"),
                "port": s.get("port"),
                "type": s.get("type"),
                "owner_or_context": s.get("owner_or_context"),
                "usage_notes": s.get("usage_notes", []),
            }
            if reveal and allow_sensitive:
                result["password"] = s.get("password", "")
                result["token"] = s.get("token", "")
                result["private_key"] = s.get("private_key", "")
                result["api_key"] = s.get("api_key", "")
            else:
                # Redact sensitive fields
                if s.get("password"):
                    result["password"] = "***"
                if s.get("token"):
                    t = s.get("token", "")
                    result["token"] = t[:10] + "***" if len(t) > 10 else "***"
                if s.get("private_key"):
                    result["private_key"] = "***"
                if s.get("api_key"):
                    result["api_key"] = "***"
            return result
    return {"error": "Secret '%s' not found" % secret_id}


def secret_context_list_aliases():
    """List all secret IDs in the vault."""
    vault = _load_vault()
    return [s.get("id") for s in vault.get("secrets", []) if s.get("id")]


def _fuzzy_match(text, secrets):
    """Fuzzy match text against secret owner/context/aliases."""
    text_lower = text.lower()
    matched = []
    for s in secrets:
        ctx = (s.get("owner_or_context", "") + " " +
               " ".join(s.get("aliases", []))).lower()
        words = [w for w in text_lower.split() if len(w) > 2]
        if any(word in ctx for word in words):
            matched.append(s)
    return matched


def pre_llm_call(messages, **kwargs):
    """
    Pre-LLM-call hook: inject redacted secret context.
    Scans user messages for server/credential references and injects
    vault context into the system prompt.
    """
    vault = _load_vault()
    secrets = vault.get("secrets", [])
    if not secrets:
        return messages

    # Scan last user message for server references
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user_text = m.get("content", "")
            break

    matched = _fuzzy_match(user_text, secrets)
    if not matched:
        return messages

    # Build injection
    parts = ["<secret-vault-context>"]
    for s in matched[:5]:
        parts.append(
            "  vault:%s: %s @ %s (%s) - %s" % (
                s["id"],
                s.get("type", "?"),
                s.get("host", "?"),
                s.get("login", "?"),
                s.get("owner_or_context", "?")
            )
        )
    parts.append("</secret-vault-context>")
    injection = "\n".join(parts)

    # Inject into system message or prepend
    for m in messages:
        if m.get("role") == "system":
            m["content"] = injection + "\n" + m["content"]
            return messages

    messages.insert(0, {"role": "system", "content": injection})
    return messages


def register(ctx):
    """Register secret-context tools and hooks."""
    ctx.register_tool(
        name="secret_context_lookup",
        toolset="secret-context",
        schema={
            "type": "object",
            "properties": {
                "secret_id": {
                    "type": "string",
                    "description": "Secret ID to look up"
                },
                "reveal": {
                    "type": "boolean",
                    "description": "If true, return full secret values"
                },
                "allow_sensitive": {
                    "type": "boolean",
                    "description": "Must be true for reveal to work"
                },
            },
            "required": ["secret_id"]
        },
        handler=lambda secret_id, reveal=False, allow_sensitive=False, **kw:
            secret_context_lookup(secret_id, reveal, allow_sensitive)
    )
    ctx.register_tool(
        name="secret_context_list_aliases",
        toolset="secret-context",
        schema={"type": "object", "properties": {}},
        handler=lambda **kw: secret_context_list_aliases()
    )
    ctx.register_hook("pre_llm_call", pre_llm_call)
