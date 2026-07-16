"""
Static validation for user-submitted bot code.

Bots run **in-process** in the game loop, so a malicious upload would run with
the same privileges as the server. This module is a best-effort guard, not a
real sandbox: it parses the code with `ast` and rejects the obvious escape
hatches before the code is ever written to disk or imported.

Rules enforced:
  * Code must parse (no syntax errors).
  * Only `melee` (and its submodules) may be imported — everything else,
    including relative imports, is rejected via AST walking.
  * Banned builtins (`eval`, `exec`, `compile`, `__import__`, `open`, ...) may
    not be referenced or called.
  * Dunder attribute access (`__globals__`, `__subclasses__`, `__class__`, ...)
    is rejected — these are the classic `().__class__.__bases__` sandbox breaks.
  * Frame / generator / traceback introspection attributes (`f_globals`,
    `gi_frame`, `cr_frame`, `tb_frame`, ...) are rejected too — they are the
    *non*-dunder gateway to the builtins dict, e.g. a generator's
    `gi_frame.f_builtins["__import__"]` reaches `__import__` without ever
    naming a dunder attribute.
  * Dunder-looking string literals (`"__globals__"`, `"__import__"`, ...) are
    rejected anywhere they appear, so reflection via `obj[<string>]` subscripts
    or `getattr`-style string keys can't smuggle a dunder past the AST check.
  * A top-level `Bot` class exposing an `act` method must be present, so a
    valid-but-useless upload fails fast here instead of at match time.

Call `validate_bot_code(code)`; it raises `BotValidationError` on the first
problem, with a message safe to show the user.
"""
import ast

# Only these top-level import roots are allowed. `melee` is the game API bots
# need; a couple of pure-stdlib maths helpers are harmless and commonly wanted.
ALLOWED_IMPORT_ROOTS = {"melee", "math", "random"}

# Builtins that enable code execution, imports, filesystem/network access, or
# reflection-based sandbox escapes. Referencing any of them (by name or call)
# rejects the upload.
BANNED_NAMES = {
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "memoryview", "exit", "quit", "help",
}

# Non-dunder attributes that expose an execution frame, its globals, or the
# builtins dict — the reflection path that reaches `__import__`/`eval` WITHOUT
# ever naming a dunder. A generator/coroutine/traceback hands you a frame
# (`gi_frame`, `cr_frame`, `tb_frame`), a frame hands you `f_builtins` /
# `f_globals`, and from there `[...]` subscripting reaches anything. Blocking
# these attribute names closes that gateway; blocking dunder string literals
# (below) closes the subscript key that would follow.
BANNED_ATTRS = {
    "f_globals", "f_builtins", "f_locals", "f_back", "f_code", "f_trace",
    "gi_frame", "gi_code", "gi_yieldfrom",
    "cr_frame", "cr_code", "cr_await",
    "ag_frame", "ag_code",
    "tb_frame", "tb_next", "tb_lasti",
    "func_globals", "func_code",
    "mro",
}

MAX_CODE_BYTES = 16 * 1024 * 1024  # 16 MiB


class BotValidationError(ValueError):
    """Raised when user bot code fails a safety or interface check."""


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _has_bot_with_act(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Bot":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "act":
                    return True
    return False


def validate_bot_code(code: str) -> None:
    """Validate user bot source. Raises BotValidationError if unsafe/invalid."""
    if not code or not code.strip():
        raise BotValidationError("Bot code is empty.")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise BotValidationError("Bot code is too large.")

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise BotValidationError(f"Syntax error: {exc.msg} (line {exc.lineno}).")

    for node in ast.walk(tree):
        # --- Imports: whitelist only ---
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORT_ROOTS:
                    raise BotValidationError(
                        f"Import of '{alias.name}' is not allowed. "
                        f"Allowed: {', '.join(sorted(ALLOWED_IMPORT_ROOTS))}."
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                raise BotValidationError("Relative imports are not allowed.")
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_IMPORT_ROOTS:
                raise BotValidationError(
                    f"Import from '{node.module}' is not allowed. "
                    f"Allowed: {', '.join(sorted(ALLOWED_IMPORT_ROOTS))}."
                )

        # --- Banned builtin names ---
        elif isinstance(node, ast.Name):
            if node.id in BANNED_NAMES:
                raise BotValidationError(f"Use of '{node.id}' is not allowed.")
            if _is_dunder(node.id):
                raise BotValidationError(f"Use of dunder name '{node.id}' is not allowed.")

        # --- Dunder / introspection attribute access (sandbox escapes) ---
        elif isinstance(node, ast.Attribute):
            if _is_dunder(node.attr):
                raise BotValidationError(
                    f"Access to dunder attribute '.{node.attr}' is not allowed."
                )
            if node.attr in BANNED_ATTRS:
                raise BotValidationError(
                    f"Access to introspection attribute '.{node.attr}' is not allowed."
                )

        # --- Dunder-looking string literals (reflection via subscript/keys) ---
        # A dunder name reachable only as a string — e.g. frame.f_builtins
        # ["__import__"] or a getattr-style key — never appears as an Attribute
        # or Name node, so catch it at the constant.
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str) and _is_dunder(node.value):
                raise BotValidationError(
                    f"Dunder string literal '{node.value}' is not allowed."
                )

    if not _has_bot_with_act(tree):
        raise BotValidationError(
            "Code must define a top-level `Bot` class with an `act` method."
        )
