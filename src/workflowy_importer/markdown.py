from __future__ import annotations

import hashlib
import html
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote

from markdown_it import MarkdownIt

from .model import ImportNode

STATE_PREFIX = ".workflowy-importer-state"
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_LIST_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-+*]|\d+[.)])\s+(?P<body>.*)$")
_TASK_RE = re.compile(r"^\[(?P<done>[ xX])\]\s*(?P<body>.*)$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(?P<lang>[^\s]*)\s*$")
_WIKI_RE = re.compile(r"(?<!\\)\[\[([^\[\]]+?)\]\]")
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

_MD = MarkdownIt("commonmark", {"html": False, "linkify": False})
_MD.enable("strikethrough")


@dataclass(slots=True)
class ParseResult:
    root: ImportNode
    files: list[Path]
    fingerprint: str


def discover_markdown(source: Path) -> list[Path]:
    source = source.expanduser().resolve()
    if source.is_file():
        if source.suffix.lower() != ".md":
            raise ValueError(f"Source file is not Markdown: {source}")
        return [source]
    if not source.is_dir():
        raise ValueError(f"Source path does not exist: {source}")

    files: list[Path] = []
    for path in source.rglob("*.md"):
        rel_parts = path.relative_to(source).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        if path.name.startswith(STATE_PREFIX):
            continue
        files.append(path)
    return sorted(files, key=lambda p: p.relative_to(source).as_posix().casefold())


def fingerprint_files(source: Path, files: Iterable[Path]) -> str:
    source = source.expanduser().resolve()
    base = source if source.is_dir() else source.parent
    digest = hashlib.sha256()
    for path in files:
        rel = path.relative_to(base).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _new_key(rel: str, line_no: int, counter: int) -> str:
    return f"node:{rel}:{line_no}:{counter}"


def _append_paragraph(
    parent: ImportNode,
    rel: str,
    line_no: int,
    counter: int,
    lines: list[str],
) -> int:
    text = " ".join(part.strip() for part in lines if part.strip())
    if text:
        parent.children.append(
            ImportNode(
                key=_new_key(rel, line_no, counter),
                name=text,
                source_file=rel,
            )
        )
        counter += 1
    return counter


def _parse_file(path: Path, rel: str) -> ImportNode:
    rel_no_suffix = str(Path(rel).with_suffix("")).replace("\\", "/")
    file_node = ImportNode(
        key=f"file:{rel}",
        name=path.stem,
        source_file=rel,
        aliases=[rel, rel_no_suffix, path.stem],
    )

    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    counter = 0
    i = 0

    if len(lines) >= 2 and lines[0].strip() == "---":
        try:
            end = next(j for j in range(1, len(lines)) if lines[j].strip() == "---")
        except StopIteration:
            end = -1
        if end > 0:
            fm = ImportNode(
                key=_new_key(rel, 1, counter),
                name="Front matter",
                source_file=rel,
            )
            counter += 1
            for off, raw in enumerate(lines[1:end], start=2):
                fm.children.append(
                    ImportNode(
                        key=_new_key(rel, off, counter),
                        name=raw if raw else " ",
                        layout="code-block",
                        source_file=rel,
                    )
                )
                counter += 1
            file_node.children.append(fm)
            i = end + 1

    heading_stack: list[tuple[int, ImportNode]] = []
    list_stack: list[tuple[int, ImportNode]] = []

    def section_parent() -> ImportNode:
        return heading_stack[-1][1] if heading_stack else file_node

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        line_no = i + 1

        if not stripped:
            list_stack.clear()
            i += 1
            continue

        fence = _FENCE_RE.match(raw)
        if fence:
            marker = fence.group(1)
            lang = fence.group("lang").strip()
            block = ImportNode(
                key=_new_key(rel, line_no, counter),
                name=f"Code ({lang})" if lang else "Code",
                source_file=rel,
            )
            counter += 1
            i += 1
            while i < len(lines):
                candidate = lines[i].lstrip()
                if candidate.startswith(marker[0] * len(marker)):
                    i += 1
                    break
                block.children.append(
                    ImportNode(
                        key=_new_key(rel, i + 1, counter),
                        name=lines[i] if lines[i] else " ",
                        layout="code-block",
                        source_file=rel,
                    )
                )
                counter += 1
                i += 1
            section_parent().children.append(block)
            list_stack.clear()
            continue

        heading = _HEADING_RE.match(raw)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            parent = heading_stack[-1][1] if heading_stack else file_node
            node = ImportNode(
                key=_new_key(rel, line_no, counter),
                name=title,
                layout="h1" if level == 1 else "h2" if level == 2 else "h3",
                source_file=rel,
                aliases=[
                    f"{rel_no_suffix}#{title}",
                    f"{rel}#{title}",
                    f"{path.stem}#{title}",
                ],
            )
            counter += 1
            parent.children.append(node)
            heading_stack.append((level, node))
            list_stack.clear()
            i += 1
            continue

        item = _LIST_RE.match(raw)
        if item:
            indent_text = item.group("indent").replace("\t", "    ")
            indent = len(indent_text)
            marker = item.group("marker")
            body = item.group("body")
            completed = False
            layout = "bullets"

            task = _TASK_RE.match(body)
            if task and marker in {"-", "+", "*"}:
                layout = "todo"
                completed = task.group("done").lower() == "x"
                body = task.group("body")
            elif marker[0].isdigit():
                body = f"{marker} {body}"

            while list_stack and list_stack[-1][0] >= indent:
                list_stack.pop()
            parent = list_stack[-1][1] if list_stack else section_parent()
            node = ImportNode(
                key=_new_key(rel, line_no, counter),
                name=body or " ",
                layout=layout,
                completed=completed,
                source_file=rel,
            )
            counter += 1
            parent.children.append(node)
            list_stack.append((indent, node))
            i += 1
            continue

        if stripped.startswith(">"):
            quote_lines: list[str] = []
            start = line_no
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                q = lines[i].lstrip()[1:]
                quote_lines.append(q[1:] if q.startswith(" ") else q)
                i += 1
            section_parent().children.append(
                ImportNode(
                    key=_new_key(rel, start, counter),
                    name=" ".join(x.strip() for x in quote_lines).strip() or " ",
                    layout="quote-block",
                    source_file=rel,
                )
            )
            counter += 1
            list_stack.clear()
            continue

        paragraph: list[str] = []
        start = line_no
        while i < len(lines):
            candidate = lines[i]
            s = candidate.strip()
            if not s:
                break
            if paragraph and (
                _HEADING_RE.match(candidate)
                or _LIST_RE.match(candidate)
                or _FENCE_RE.match(candidate)
                or candidate.lstrip().startswith(">")
            ):
                break
            paragraph.append(candidate)
            i += 1

        counter = _append_paragraph(section_parent(), rel, start, counter, paragraph)
        list_stack.clear()
        if i < len(lines) and not lines[i].strip():
            i += 1

    return file_node


def build_tree(source: Path, root_name: str) -> ParseResult:
    source = source.expanduser().resolve()
    files = discover_markdown(source)
    if not files:
        raise ValueError("No Markdown files found.")

    base = source if source.is_dir() else source.parent
    root = ImportNode(key="import:root", name=root_name)
    dirs: dict[str, ImportNode] = {"": root}

    for path in files:
        rel = path.relative_to(base).as_posix()
        parent = root
        current_parts: list[str] = []

        for part in Path(rel).parent.parts:
            if part == ".":
                continue
            current_parts.append(part)
            key = "/".join(current_parts)
            if key not in dirs:
                dirs[key] = ImportNode(key=f"dir:{key}", name=part)
                parent.children.append(dirs[key])
            parent = dirs[key]

        parent.children.append(_parse_file(path, rel))

    return ParseResult(
        root=root,
        files=files,
        fingerprint=fingerprint_files(source, files),
    )


def _norm_alias(value: str) -> str:
    value = unquote(value).strip().replace("\\", "/")
    value = re.sub(r"/+", "/", value)
    value = re.sub(r"\s+", " ", value)
    return value.casefold()


class LinkResolver:
    def __init__(self, root: ImportNode):
        self._aliases: dict[str, set[str]] = {}
        for node in root.walk():
            for alias in node.aliases:
                self._aliases.setdefault(_norm_alias(alias), set()).add(node.key)

    def resolve_key(self, target: str, source_file: str | None) -> str | None:
        raw = unquote(target).strip().replace("\\", "/")
        candidates: list[str] = []

        if raw.startswith("#") and source_file:
            source_no_suffix = str(Path(source_file).with_suffix("")).replace("\\", "/")
            candidates.extend([f"{source_no_suffix}{raw}", f"{source_file}{raw}"])
        else:
            path_part, sep, anchor = raw.partition("#")
            if source_file and (path_part.startswith(".") or path_part.endswith(".md")):
                joined = posixpath.normpath(
                    posixpath.join(posixpath.dirname(source_file), path_part)
                )
                candidates.append(joined + (f"#{anchor}" if sep else ""))
                if joined.endswith(".md"):
                    candidates.append(joined[:-3] + (f"#{anchor}" if sep else ""))
            candidates.append(raw)
            if path_part.endswith(".md"):
                candidates.append(path_part[:-3] + (f"#{anchor}" if sep else ""))

        seen: set[str] = set()
        for candidate in candidates:
            norm = _norm_alias(candidate)
            if norm in seen:
                continue
            seen.add(norm)
            keys = self._aliases.get(norm, set())
            if len(keys) == 1:
                return next(iter(keys))
            if len(keys) > 1:
                return None
        return None

    @staticmethod
    def workflowy_url(node_id: str) -> str:
        compact = node_id.replace("-", "")
        short = compact[-12:] if len(compact) >= 12 else compact
        return f"https://workflowy.com/#/{short}"

    def href(
        self,
        target: str,
        source_file: str | None,
        ids: dict[str, str],
    ) -> str | None:
        key = self.resolve_key(target, source_file)
        node_id = ids.get(key) if key else None
        return self.workflowy_url(node_id) if node_id else None


def render_inline(
    text: str,
    resolve_target: Callable[[str], str | None] | None = None,
) -> str:
    placeholders: dict[str, str] = {}

    def protect_image(match: re.Match[str]) -> str:
        token = f"WFYIMGTOKEN{len(placeholders):06d}"
        placeholders[token] = html.escape(match.group(0), quote=False)
        return token

    work = _IMAGE_RE.sub(protect_image, text)

    def wiki(match: re.Match[str]) -> str:
        inner = match.group(1)
        target, sep, label = inner.partition("|")
        target = target.strip()
        display = (label if sep else target).strip()
        href = resolve_target(target) if resolve_target else None
        if not href:
            return match.group(0)
        token = f"WFYLINKTOKEN{len(placeholders):06d}"
        placeholders[token] = (
            f'<a href="{html.escape(href, quote=True)}">{html.escape(display)}</a>'
        )
        return token

    work = _WIKI_RE.sub(wiki, work)

    if resolve_target:

        def md_link(match: re.Match[str]) -> str:
            label, target = match.group(1), match.group(2)
            target_lower = target.lower()
            if not (
                target.startswith("#")
                or target_lower.endswith(".md")
                or ".md#" in target_lower
            ):
                return match.group(0)
            href = resolve_target(target)
            return f"[{label}]({href})" if href else match.group(0)

        work = _MD_LINK_RE.sub(md_link, work)

    rendered = _MD.renderInline(work).strip()
    rendered = (
        rendered.replace("<strong>", "<b>")
        .replace("</strong>", "</b>")
        .replace("<em>", "<i>")
        .replace("</em>", "</i>")
        .replace("<del>", "<s>")
        .replace("</del>", "</s>")
    )
    for token, replacement in placeholders.items():
        rendered = rendered.replace(token, replacement)
    return rendered


def count_links(root: ImportNode, resolver: LinkResolver) -> tuple[int, int]:
    resolved = 0
    unresolved = 0

    for node in root.walk():
        for match in _WIKI_RE.finditer(node.name):
            target = match.group(1).partition("|")[0].strip()
            if resolver.resolve_key(target, node.source_file):
                resolved += 1
            else:
                unresolved += 1

        for match in _MD_LINK_RE.finditer(node.name):
            target = match.group(2)
            target_lower = target.lower()
            if (
                target.startswith("#")
                or target_lower.endswith(".md")
                or ".md#" in target_lower
            ):
                if resolver.resolve_key(target, node.source_file):
                    resolved += 1
                else:
                    unresolved += 1

    return resolved, unresolved


def preview_tree(root: ImportNode, limit: int = 100) -> str:
    lines: list[str] = []
    seen = 0

    def visit(node: ImportNode, depth: int) -> None:
        nonlocal seen
        if seen >= limit:
            return
        suffix = " [done]" if node.completed else ""
        lines.append(f"{'  ' * depth}- {node.name}{suffix}")
        seen += 1
        for child in node.children:
            visit(child, depth + 1)

    visit(root, 0)
    total = sum(1 for _ in root.walk())
    if total > seen:
        lines.append(f"... {total - seen} more nodes")
    return "\n".join(lines)
