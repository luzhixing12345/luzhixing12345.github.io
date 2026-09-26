"""Render markdown with MarkdownParser and highlight code with syntaxlight."""

from __future__ import annotations

import html
import re
import types
from dataclasses import dataclass
from pathlib import Path

import MarkdownParser
import syntaxlight

_MARKDOWN = MarkdownParser.Markdown()
_LANGUAGES: set[str] = set()
_TOKEN_SPAN = re.compile(r'<span class="([^"]*)">(.*?)</span>', re.DOTALL)
_CSS_ROOT = Path(syntaxlight.__file__).resolve().parent / "css"

_ALIASES = {
    "bash": "shell",
    "sh": "shell",
    "zsh": "shell",
    "git bash": "shell",
    "yml": "yaml",
    "yaml": "yaml",
    "py": "python",
    "text": "txt",
    "plaintext": "txt",
}


@dataclass
class Document:
    html: str
    title: str
    toc: str
    has_mermaid: bool


def reset_languages() -> None:
    _LANGUAGES.clear()


def export_code_css(directory: Path) -> list[str]:
    languages = sorted(language for language in _LANGUAGES if (_CSS_ROOT / f"{language}.css").exists())
    if languages:
        syntaxlight.export_css(languages, str(directory))
    return languages


def excerpt(text: str, limit: int = 88) -> str:
    chunks: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("```") or stripped.startswith("!["):
            continue
        if stripped.startswith("|") or stripped.startswith(">"):
            continue
        chunks.append(re.sub(r"[*_`~\[\]]", "", stripped))
        if sum(len(item) for item in chunks) >= limit:
            break
    summary = " ".join(chunks).strip()
    if len(summary) > limit:
        summary = summary[: limit - 1].rstrip() + "…"
    return summary


MERMAID_SCRIPT = """<script type="module">
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
mermaid.initialize({ startOnLoad: true, theme: "neutral", securityLevel: "loose" });
</script>
"""


def parse_markdown(text: str, file_path: str = "") -> Document:
    lines = _MARKDOWN.preprocess_parser(text)
    root = _MARKDOWN.block_parser(lines)
    tree = _MARKDOWN.tree_parser(root)
    mermaid = _highlight(tree, file_path)
    toc = _toc_inner(_MARKDOWN.get_toc(tree))
    body = "".join(block.to_html() for block in tree.sub_blocks)
    return Document(body, _title(tree), toc, mermaid)


def _title(tree) -> str:
    for block in tree.sub_blocks:
        if block.block_name == "HashHeaderBlock" and block.input.get("level") == 1:
            return block.to_display_word().strip()
    return ""


def _toc_inner(toc: str) -> str:
    prefix = '<div class="header-navigator">'
    if toc.startswith(prefix) and toc.endswith("</div>"):
        return toc[len(prefix) : -len("</div>")]
    return toc


def _highlight(tree, file_path: str) -> bool:
    mermaid = False
    for block in tree.sub_blocks:
        if block.block_name == "CodeBlock":
            if _highlight_code(block, file_path):
                mermaid = True
        elif _highlight(block, file_path):
            mermaid = True
    return mermaid


def _highlight_code(block, file_path: str) -> bool:
    language = (block.input.get("language") or "UNKNOWN").strip()
    if language.lower() == "mermaid":
        source = html.escape(block.input.get("code") or "")

        def mermaid_html(self, _source=source):
            return f'<div class="mermaid">{_source}</div>'

        block.to_html = types.MethodType(mermaid_html, block)
        return True

    mapped = _ALIASES.get(language.lower(), language)
    if mapped != "UNKNOWN" and not syntaxlight.is_language_support(mapped):
        return False
    if mapped == "UNKNOWN":
        mapped = "txt"
    else:
        mapped = syntaxlight.clean_language(mapped)
    block.input["language"] = mapped
    source = block.input.get("code") or ""
    if not source.strip():
        return False
    try:
        result = syntaxlight.parse(source, mapped, file_path or None)
        lines, tokens = _highlight_marks(block.input.get("append_text"))
        highlighted = _trim_indent_highlight(result.parser.to_html(highlight_lines=lines, highlight_tokens=tokens))
    except Exception as exc:
        print(f"highlight failed: {file_path or 'markdown'}: {mapped}: {exc}", flush=True)
        return False
    if result.error is not None:
        print(f"highlight warning: {file_path or 'markdown'}: {mapped}: {result.error}", flush=True)
    block.input["code"] = highlighted

    def code_html(self):
        return f'<pre class="language-{self.input["language"]}"><code>{self.input["code"]}</code></pre>'

    block.to_html = types.MethodType(code_html, block)
    _LANGUAGES.add(mapped)
    return False


def _highlight_marks(append_text: str | None) -> tuple[list[int], list[int]]:
    if not append_text or append_text in {"?", "??"}:
        return [], []
    lines: list[int] = []
    tokens: list[int] = []
    for item in append_text.split(","):
        item = item.strip()
        if not item:
            continue
        target = tokens if item.startswith("#") else lines
        item = item[1:] if item.startswith("#") else item
        if "-" in item:
            start, end = item.split("-", 1)
            target.extend(range(int(start), int(end) + 1))
        else:
            target.append(int(item))
    return lines, tokens


def _trim_indent_highlight(code_html: str) -> str:
    at_line_start = True

    def update(match: re.Match[str]) -> str:
        nonlocal at_line_start
        classes = match.group(1).split()
        content = match.group(2)
        if at_line_start and "HighlightLine" in classes and "SPACE" in classes:
            classes.remove("HighlightLine")
        if "\n" in content:
            trailing = content.rsplit("\n", 1)[1]
            at_line_start = not trailing or trailing.isspace()
        elif content and not content.isspace():
            at_line_start = False
        return f'<span class="{" ".join(classes)}">{content}</span>'

    return _TOKEN_SPAN.sub(update, code_html)
