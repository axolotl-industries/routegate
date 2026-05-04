"""Caddyfile parser/writer for the small subset routegate manages.

Supported managed-route shape:

    [http://]hostname {
        [import authelia]
        [other directives ...]
        reverse_proxy host:port [{ option lines }]
    }

Anything else at the top level (the global `{ ... }` block, named snippets like
`(authelia) { ... }`, multi-host headers, blocks without a top-level
`reverse_proxy`, and route bodies that contain unrecognized nested directives)
is preserved verbatim as a PreservedBlock.

The writer emits managed routes in a canonical layout. Round-trip equality is
defined at the object level (parse -> dump -> parse yields equal objects),
not byte level.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

INDENT = "    "

_ROUTE_HEADER_RE = re.compile(
    r"^(?:(?P<scheme>https?)://)?(?P<hostname>[A-Za-z0-9][A-Za-z0-9.\-]*)$"
)


@dataclass
class Route:
    hostname: str
    scheme: str = "http"
    target: str = ""
    authelia: bool = False
    reverse_proxy_options: list[str] = field(default_factory=list)
    extra_lines: list[str] = field(default_factory=list)
    leading: str = "\n"


@dataclass
class PreservedBlock:
    leading: str
    raw: str


@dataclass
class CaddyDoc:
    items: list = field(default_factory=list)
    trailing: str = ""

    @property
    def routes(self) -> list[Route]:
        return [i for i in self.items if isinstance(i, Route)]

    def find_route(self, hostname: str) -> Route | None:
        for item in self.items:
            if isinstance(item, Route) and item.hostname == hostname:
                return item
        return None

    def remove_route(self, hostname: str) -> bool:
        for idx, item in enumerate(self.items):
            if isinstance(item, Route) and item.hostname == hostname:
                del self.items[idx]
                return True
        return False

    def add_route(self, route: Route) -> None:
        self.items.append(route)


def load(path: Path | str) -> CaddyDoc:
    return parse(Path(path).read_text())


def save(doc: CaddyDoc, path: Path | str) -> None:
    Path(path).write_text(dump(doc))


def parse(text: str) -> CaddyDoc:
    items: list = []
    cursor = 0
    for open_pos, close_pos in _tokenize_top_level(text):
        prefix = text[cursor:open_pos]
        last_nl = prefix.rfind("\n")
        if last_nl == -1:
            leading = ""
            header_line = prefix
        else:
            leading = prefix[: last_nl + 1]
            header_line = prefix[last_nl + 1 :]
        header = header_line.rstrip()
        body = text[open_pos + 1 : close_pos]
        raw = header_line + text[open_pos : close_pos + 1]
        items.append(_classify(leading, header, body, raw))
        cursor = close_pos + 1
    return CaddyDoc(items=items, trailing=text[cursor:])


def dump(doc: CaddyDoc) -> str:
    parts: list[str] = []
    for item in doc.items:
        if isinstance(item, PreservedBlock):
            parts.append(item.leading)
            parts.append(item.raw)
        else:
            parts.append(item.leading)
            parts.append(_emit_route(item))
    parts.append(doc.trailing)
    return "".join(parts)


def _tokenize_top_level(text: str) -> list[tuple[int, int]]:
    """Return (open_brace_idx, close_brace_idx) pairs for each top-level block."""
    blocks: list[tuple[int, int]] = []
    depth = 0
    in_comment = False
    in_string = False
    string_char = ""
    open_pos = -1
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_comment:
            if c == "\n":
                in_comment = False
            i += 1
            continue
        if in_string:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == string_char:
                in_string = False
            i += 1
            continue
        if c == "#":
            in_comment = True
        elif c in ('"', "'"):
            in_string = True
            string_char = c
        elif c == "{":
            if depth == 0:
                open_pos = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                blocks.append((open_pos, i))
            elif depth < 0:
                raise ValueError(f"unbalanced '}}' at offset {i}")
        i += 1
    if depth != 0:
        raise ValueError(f"unbalanced braces (final depth={depth})")
    return blocks


def _classify(leading: str, header: str, body: str, raw: str):
    m = _ROUTE_HEADER_RE.match(header)
    if not m:
        return PreservedBlock(leading=leading, raw=raw)
    parsed = _parse_route_body(body)
    if parsed is None:
        return PreservedBlock(leading=leading, raw=raw)
    authelia, target, rp_opts, extras = parsed
    if not target:
        return PreservedBlock(leading=leading, raw=raw)
    return Route(
        hostname=m.group("hostname"),
        scheme=m.group("scheme") or "",
        target=target,
        authelia=authelia,
        reverse_proxy_options=rp_opts,
        extra_lines=extras,
        leading=leading,
    )


def _parse_route_body(
    body: str,
) -> tuple[bool, str | None, list[str], list[str]] | None:
    """Parse a route body. Returns None if the body has unrecognized nested
    directives (in which case the caller should preserve the whole block)."""
    state = {"authelia": False, "target": None, "extras": []}
    rp_opts: list[str] = []
    cursor = 0
    for open_pos, close_pos in _tokenize_top_level(body):
        last_nl = body.rfind("\n", cursor, open_pos)
        head_start = cursor if last_nl == -1 else last_nl + 1
        _consume_simple_lines(body[cursor:head_start], state)
        header_line = body[head_start:open_pos].rstrip()
        tokens = header_line.split()
        nested_body = body[open_pos + 1 : close_pos]
        if tokens and tokens[0] == "reverse_proxy" and len(tokens) >= 2:
            state["target"] = tokens[1]
            rp_opts = _dedent_lines(nested_body)
        else:
            return None
        cursor = close_pos + 1
    _consume_simple_lines(body[cursor:], state)
    return state["authelia"], state["target"], rp_opts, state["extras"]


def _consume_simple_lines(text: str, state: dict) -> None:
    for line in text.split("\n"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        tokens = s.split()
        if tokens[0] == "import" and len(tokens) >= 2 and tokens[1] == "authelia":
            state["authelia"] = True
        elif tokens[0] == "reverse_proxy" and len(tokens) >= 2:
            state["target"] = tokens[1]
        else:
            state["extras"].append(s)


def _dedent_lines(text: str) -> list[str]:
    return [line.strip() for line in text.split("\n") if line.strip()]


def _emit_route(r: Route) -> str:
    scheme_prefix = f"{r.scheme}://" if r.scheme else ""
    lines = [f"{scheme_prefix}{r.hostname} {{"]
    if r.authelia:
        lines.append(f"{INDENT}import authelia")
    for extra in r.extra_lines:
        lines.append(f"{INDENT}{extra}")
    if r.reverse_proxy_options:
        lines.append(f"{INDENT}reverse_proxy {r.target} {{")
        for opt in r.reverse_proxy_options:
            lines.append(f"{INDENT}{INDENT}{opt}")
        lines.append(f"{INDENT}}}")
    elif r.target:
        lines.append(f"{INDENT}reverse_proxy {r.target}")
    lines.append("}")
    return "\n".join(lines)
