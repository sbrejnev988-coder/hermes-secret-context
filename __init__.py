#!/usr/bin/env python3
"""Patch Hermes secret-context plugin handlers to return JSON strings.

Hermes/OpenAI-compatible tool messages require string content.  This patcher
finds the registrations for secret_context_lookup and secret_context_search and
wraps their handlers without changing the plugin's lookup/reveal semantics.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

TARGET_TOOLS = {"secret_context_lookup", "secret_context_search"}
WRAPPER = "_hermes_string_tool_handler"
MARKER = "# HERMES-SECRET-CONTEXT-STRING-RESULT-r5"
HELPER = r'''

# HERMES-SECRET-CONTEXT-STRING-RESULT-r5
# Hermes tool result content must be a string for strict OpenAI-compatible
# providers.  Preserve the original payload as JSON instead of returning a dict.
def _hermes_json_tool_result(payload):
    import json as _hermes_json
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8", "replace")
    return _hermes_json.dumps(
        payload,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )


def _hermes_string_tool_handler(handler):
    import functools as _hermes_functools
    import inspect as _hermes_inspect

    if _hermes_inspect.iscoroutinefunction(handler):
        @_hermes_functools.wraps(handler)
        async def _hermes_async_handler(*args, **kwargs):
            return _hermes_json_tool_result(await handler(*args, **kwargs))
        return _hermes_async_handler

    @_hermes_functools.wraps(handler)
    def _hermes_sync_handler(*args, **kwargs):
        result = handler(*args, **kwargs)
        if _hermes_inspect.isawaitable(result):
            async def _hermes_awaited_result():
                return _hermes_json_tool_result(await result)
            return _hermes_awaited_result()
        return _hermes_json_tool_result(result)
    return _hermes_sync_handler
'''.rstrip() + "\n"


def _candidate_files(home: Path) -> Iterable[Path]:
    explicit = os.environ.get("MEMORY_WIKI_SECRET_CONTEXT_PLUGIN", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        yield path / "__init__.py" if path.is_dir() else path
        return
    roots = [home / "plugins"]
    profiles = home / "profiles"
    if profiles.is_dir():
        roots.extend(p / "plugins" for p in profiles.iterdir() if p.is_dir())
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/__init__.py")):
            if "memory-wiki" in path.parent.name.lower():
                continue
            yield path


def discover(home: Optional[Path] = None) -> Optional[Path]:
    home = Path(home or os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()
    for path in _candidate_files(home):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if all(name in text for name in TARGET_TOOLS) and "register_tool" in text:
            return path
    return None


def _const_string(node: ast.AST) -> str:
    return str(node.value) if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _call_name(call: ast.Call) -> str:
    for keyword in call.keywords:
        if keyword.arg == "name":
            return _const_string(keyword.value)
    return _const_string(call.args[0]) if call.args else ""


def _is_register_tool(call: ast.Call) -> bool:
    func = call.func
    return isinstance(func, ast.Attribute) and func.attr == "register_tool"


def _handler_node(call: ast.Call, function_names: set[str]) -> Optional[ast.AST]:
    for keyword in call.keywords:
        if keyword.arg == "handler":
            return keyword.value
    # Positional fallback. Prefer a named function declared in this module;
    # schemas/toolsets are often dicts or strings, while the handler is a def.
    candidates = list(call.args[1:])
    for node in reversed(candidates):
        if isinstance(node, ast.Name) and node.id in function_names:
            return node
        if isinstance(node, (ast.Lambda, ast.Attribute)):
            return node
    return candidates[-1] if candidates else None


def _line_data(text: str) -> Tuple[List[str], List[int]]:
    lines = text.splitlines(keepends=True)
    if not lines:
        lines = [""]
    starts: List[int] = []
    cursor = 0
    for line in lines:
        starts.append(cursor)
        cursor += len(line)
    return lines, starts


def _char_column(line: str, utf8_byte_column: int) -> int:
    # CPython AST columns are UTF-8 byte offsets, not Unicode code-point offsets.
    raw = line.encode("utf-8")[:max(0, int(utf8_byte_column))]
    return len(raw.decode("utf-8", "ignore"))


def _span(node: ast.AST, lines: Sequence[str], starts: Sequence[int]) -> Tuple[int, int]:
    if not all(hasattr(node, attr) for attr in ("lineno", "col_offset", "end_lineno", "end_col_offset")):
        raise ValueError("python_ast_missing_end_positions")
    start_line = int(node.lineno) - 1
    end_line = int(node.end_lineno) - 1
    start = starts[start_line] + _char_column(lines[start_line], int(node.col_offset))
    end = starts[end_line] + _char_column(lines[end_line], int(node.end_col_offset))
    return start, end


def analyze_text(text: str) -> Dict[str, Any]:
    tree = ast.parse(text)
    function_names = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    lines, starts = _line_data(text)
    found: Dict[str, Dict[str, Any]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_register_tool(node):
            continue
        name = _call_name(node)
        if name not in TARGET_TOOLS:
            continue
        handler = _handler_node(node, function_names)
        if handler is None:
            found[name] = {"found": True, "error": "handler_not_resolved"}
            continue
        start, end = _span(handler, lines, starts)
        segment = text[start:end]
        wrapped = segment.lstrip().startswith(WRAPPER + "(")
        found[name] = {
            "found": True,
            "wrapped": wrapped,
            "handler": segment[:200],
            "start": start,
            "end": end,
        }
    return {
        "tools": found,
        "missing": sorted(TARGET_TOOLS - set(found)),
        "helper_present": MARKER in text,
        "complete": not (TARGET_TOOLS - set(found)),
        "patched": not (TARGET_TOOLS - set(found)) and MARKER in text and all(v.get("wrapped") for v in found.values()),
    }


def patch_text(text: str) -> Tuple[str, Dict[str, Any]]:
    report = analyze_text(text)
    if report["missing"]:
        raise RuntimeError("target_tool_registration_missing:" + ",".join(report["missing"]))
    unresolved = [name for name, item in report["tools"].items() if item.get("error")]
    if unresolved:
        raise RuntimeError("target_handler_unresolved:" + ",".join(unresolved))
    replacements: List[Tuple[int, int, str]] = []
    for name, item in report["tools"].items():
        if item.get("wrapped"):
            continue
        start, end = int(item["start"]), int(item["end"])
        replacements.append((start, end, f"{WRAPPER}({text[start:end]})"))
    patched = text
    for start, end, replacement in sorted(replacements, reverse=True):
        patched = patched[:start] + replacement + patched[end:]
    if MARKER not in patched:
        patched = patched.rstrip() + HELPER
    final = analyze_text(patched)
    if not final["patched"]:
        raise RuntimeError("post_patch_validation_failed")
    final["changed"] = patched != text
    return patched, final


def patch_file(path: Path, apply: bool) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if apply:
        patched, report = patch_text(text)
        if patched != text:
            tmp = path.with_name(path.name + ".r5.tmp")
            tmp.write_text(patched, encoding="utf-8")
            compile(patched, str(path), "exec")
            os.replace(tmp, path)
        report.update({"path": str(path), "mode": "apply"})
        return report
    report = analyze_text(text)
    report.update({"path": str(path), "mode": "check", "needs_patch": not report["patched"]})
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", help="secret-context plugin __init__.py or directory")
    parser.add_argument("--home", help="Hermes home, default ~/.hermes")
    parser.add_argument("--find", action="store_true", help="print discovered plugin path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    home = Path(args.home).expanduser() if args.home else None
    if args.path:
        path = Path(args.path).expanduser()
        path = path / "__init__.py" if path.is_dir() else path
    else:
        path = discover(home)
    if path is None:
        print(json.dumps({"ok": False, "error": "secret_context_plugin_not_found"}, ensure_ascii=False))
        return 2
    if args.find:
        print(str(path))
        return 0
    try:
        report = patch_file(path, apply=bool(args.apply))
        report["ok"] = True
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if (args.apply or report.get("patched")) else 3
    except Exception as exc:
        print(json.dumps({"ok": False, "path": str(path), "error": f"{type(exc).__name__}:{exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
