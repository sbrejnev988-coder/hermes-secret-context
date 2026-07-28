# Hermes Secret Context Plugin

Pre-LLM-call hook that scans user messages for server/credential references and injects redacted vault context into the system prompt.

## Features
- `secret_context_lookup(secret_id, reveal=False)` — Look up a secret (redacted by default)
- `secret_context_list_aliases()` — List all secret IDs
- `pre_llm_call` hook — Auto-inject vault context on server mentions

## Security
- NEVER injects raw passwords/tokens into LLM context
- All secrets redacted unless `reveal=True + allow_sensitive=True`
- Reads from `<HERMES_HOME>/vault/secrets_registry.json`
- secrets_registry.json: chmod 0600

## Usage
```python
# In Hermes chat:
# "server Evgeniy" → auto-injects redacted vault context
# secret_context_lookup("server.evgeniy.main") → returns host/login/port (redacted)
# secret_context_lookup("server.evgeniy.main", reveal=True, allow_sensitive=True) → full secret
```