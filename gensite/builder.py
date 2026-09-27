"""Build the static site in docs/ from blog posts, note.md, tools.md, and travel notes."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from gensite.map import load_provinces, map_viewbox, short_name
from gensite.markdown_html import MERMAID_SCRIPT, excerpt, export_code_css, parse_markdown, reset_languages

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
THEME = ROOT / "theme"
CHINA = ROOT / "data" / "china.json"
SITE_NAME = "陆知行"
_LIVE = ""
_LIVE_CLIENTS: set = set()
_LIVE_LOOP: asyncio.AbstractEventLoop | None = None

_LIVE_SCRIPT = """<script>
(function () {
  var socket = new WebSocket("ws://127.0.0.1:WS_PORT");
  socket.onmessage = function (event) {
    if (event.data === "reload") location.reload();
  };
})();
</script>
"""


def fill(template: str, mapping: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return mapping[key] if key in mapping else match.group(0)

    return re.sub(r"\{\{([A-Z0-9_]+)\}\}", repl, template)


def encode_rel(rel: str) -> str:
    parts = []
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            parts.append(part)
        else:
            parts.append(quote(part, safe=""))
    return "/".join(parts)


def href_between(from_dir: Path, target: Path) -> str:
    return encode_rel(os.path.relpath(target, from_dir).replace("\\", "/"))


def dir_href(from_dir: Path, to_dir: Path) -> str:
    rel = os.path.relpath(to_dir, from_dir).replace("\\", "/")
    if rel == ".":
        rel = "./"
    elif not rel.endswith("/"):
        rel += "/"
    return encode_rel(rel)


def git_dates() -> dict[str, str]:
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", "log", "--format=%cs", "--name-only"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return {}
    dates: dict[str, str] = {}
    current = ""
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stripped):
            current = stripped
            continue
        if stripped and current and stripped not in dates:
            dates[stripped.replace("\\", "/")] = current
    return dates


def file_date(path: Path, dates: dict[str, str]) -> str:
    rel = path.relative_to(ROOT).as_posix()
    if rel in dates:
        return dates[rel]
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _parse_tags(text: str) -> list[str]:
    body = text.strip()
    if body.startswith("[") and body.endswith("]"):
        body = body[1:-1]
    tags: list[str] = []
    for part in body.split(","):
        tag = _unquote(part.strip())
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def load_blog_config(path: Path) -> dict[str, dict[str, object]]:
    """Read blog/posts.yaml. Each article is a filename key with date and tags."""
    posts: dict[str, dict[str, object]] = {}
    if not path.exists():
        return posts
    current: dict[str, object] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[0] not in " \t":
            key = _unquote(raw.strip()[:-1].strip() if raw.strip().endswith(":") else raw.strip())
            current = {"date": "", "tags": [], "public": False}
            posts[key] = current
            continue
        if current is None:
            continue
        field, _, value = raw.strip().partition(":")
        if field == "date":
            current["date"] = value.strip()
        elif field == "tags":
            current["tags"] = _parse_tags(value)
        elif field == "public":
            current["public"] = value.strip().lower() == "true"
    return posts


def _config_block(name: str, date: str, tags: list[str], public: bool = False) -> str:
    quoted = json.dumps(name, ensure_ascii=False)
    tag_text = ", ".join(json.dumps(tag, ensure_ascii=False) for tag in tags)
    flag = "true" if public else "false"
    return f"{quoted}:\n  date: {date}\n  tags: [{tag_text}]\n  public: {flag}\n"


def ensure_blog_config(files: list[Path]) -> dict[str, dict[str, object]]:
    path = ROOT / "blog" / "posts.yaml"
    legacy = ROOT / "blog" / "time.yaml"
    config = load_blog_config(path)
    if legacy.exists():
        for name, date in load_legacy_times(legacy).items():
            entry = config.setdefault(name, {"date": date, "tags": []})
            if not entry.get("date"):
                entry["date"] = date
    today = datetime.now().strftime("%Y-%m-%d")
    missing = [item.name for item in files if item.name not in config]
    for name in missing:
        config[name] = {"date": today, "tags": [], "public": False}
    header = (
        "# 博客文章配置。\n"
        "# 已有条目的 date 不会在构建时改写。\n"
        "# 新文章会追加到末尾：date 为当天，tags 留空，public 为 false。\n"
        "# public 为 true 才出现在站点上。省略或 false 都不生成页面。\n"
        "# tags 用方括号列出，点击标签会打开同标签的文章列表。\n\n"
    )
    if not path.exists():
        known = list(config)
        blocks = [_config_block(name, str(config[name].get("date") or today), list(config[name].get("tags") or []), bool(config[name].get("public"))) for name in known]
        path.write_text(header + "\n".join(blocks), encoding="utf-8")
    elif missing:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(_config_block(name, today, []) for name in missing))
    if legacy.exists():
        legacy.unlink()
    return config


def load_legacy_times(path: Path) -> dict[str, str]:
    times: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r'^(?:"([^"]+)"|(\S+))\s*:\s*(\d{4}-\d{2}-\d{2})\s*$', line)
        if match:
            times[match.group(1) or match.group(2)] = match.group(3)
    return times


@dataclass
class Post:
    title: str
    stem: str
    date: str
    tags: tuple[str, ...]
    summary: str
    html: str
    toc: str
    has_mermaid: bool


@dataclass
class Trip:
    province: str
    city: str | None
    segments: tuple[str, ...]
    title: str
    date: str
    summary: str
    html: str
    has_mermaid: bool

    @property
    def place(self) -> str:
        if self.city:
            return f"{self.province} · {self.city}"
        return self.province

    def out_dir(self) -> Path:
        return (DOCS / "travel").joinpath(*self.segments)

    def href_from_travel(self) -> str:
        rel = self.out_dir().relative_to(DOCS / "travel").as_posix()
        return encode_rel(rel + "/")


def collect_posts() -> list[Post]:
    blog = ROOT / "blog"
    if not blog.exists():
        return []
    files = sorted(path for path in blog.rglob("*.md") if path.is_file())
    meta = ensure_blog_config(files)
    used: set[str] = set()
    posts: list[Post] = []
    for path in files:
        stem = path.stem
        if stem in used:
            stem = path.relative_to(blog).with_suffix("").as_posix().replace("/", "-")
        used.add(stem)
        text = path.read_text(encoding="utf-8")
        doc = parse_markdown(text, str(path))
        entry = meta.get(path.name) or {}
        if not entry.get("public"):
            continue
        tags = tuple(tag for tag in entry.get("tags") or [] if isinstance(tag, str))
        posts.append(
            Post(
                title=doc.title or stem,
                stem=stem,
                date=str(entry.get("date") or "") or file_date(path, {}),
                tags=tags,
                summary=excerpt(text),
                html=doc.html,
                toc=doc.toc,
                has_mermaid=doc.has_mermaid,
            )
        )
    posts.sort(key=lambda item: item.title)
    posts.sort(key=lambda item: item.date, reverse=True)
    return posts


def collect_trips(dates: dict[str, str]) -> list[Trip]:
    root = ROOT / "travel"
    trips: list[Trip] = []
    if not root.exists():
        return trips
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        folders = rel.parts[:-1]
        if not folders:
            continue
        province = folders[0]
        city = folders[1] if len(folders) > 1 else None
        if path.stem.lower() == "readme":
            segments = folders
        else:
            segments = folders + (path.stem,)
        text = path.read_text(encoding="utf-8")
        doc = parse_markdown(text, str(path))
        title = doc.title or city or province
        trips.append(
            Trip(
                province=province,
                city=city,
                segments=segments,
                title=title,
                date=file_date(path, dates),
                summary=excerpt(text),
                html=doc.html,
                has_mermaid=doc.has_mermaid,
            )
        )
    trips.sort(key=lambda item: item.place)
    trips.sort(key=lambda item: item.date, reverse=True)
    return trips


def linkify(fragment: str) -> str:
    parts = re.split(r"(<[^>]+>)", fragment)
    in_anchor = 0
    in_skip = 0
    url_re = re.compile(r"https?://[^\s<]+")

    def repl(match: re.Match[str]) -> str:
        raw = match.group(0).rstrip(".,;:)")
        tail = match.group(0)[len(raw) :]
        href = html.escape(raw, quote=True)
        return f'<a href="{href}" target="_blank">{href}</a>{tail}'

    out: list[str] = []
    for part in parts:
        if part.startswith("<"):
            low = part.lower()
            if low.startswith("<a ") or low == "<a>" or low.startswith("<a>"):
                in_anchor += 1
            elif low.startswith("</a"):
                in_anchor = max(0, in_anchor - 1)
            elif low.startswith("<pre") or low.startswith("<code"):
                in_skip += 1
            elif low.startswith("</pre") or low.startswith("</code"):
                in_skip = max(0, in_skip - 1)
            out.append(part)
        elif in_anchor or in_skip:
            out.append(part)
        else:
            out.append(url_re.sub(repl, part))
    return "".join(out)


def markdown_page(path: Path, fallback: str) -> tuple[str, bool]:
    if not path.exists():
        return f'<article class="prose"><p>{html.escape(fallback)}</p></article>', False
    doc = parse_markdown(path.read_text(encoding="utf-8"), str(path))
    return f'<article class="prose">{linkify(doc.html)}</article>', doc.has_mermaid


def render_header(from_dir: Path, active: str) -> str:
    template = (THEME / "header.html").read_text(encoding="utf-8")

    def on(name: str) -> str:
        return "active" if name == active else ""

    return fill(
        template,
        {
            "HOME": dir_href(from_dir, DOCS),
            "NOTES": dir_href(from_dir, DOCS / "notes"),
            "TOOLS": dir_href(from_dir, DOCS / "tools"),
            "TRAVEL": dir_href(from_dir, DOCS / "travel"),
            "ABOUT": dir_href(from_dir, DOCS / "about"),
            "ON_BLOG": on("blog"),
            "ON_NOTES": on("notes"),
            "ON_TOOLS": on("tools"),
            "ON_TRAVEL": on("travel"),
            "ON_ABOUT": on("about"),
        },
    )


def asset_map(from_dir: Path) -> dict[str, str]:
    return {
        "ASSET_SITE_CSS": href_between(from_dir, DOCS / "css" / "site.css"),
        "ASSET_SITE_JS": href_between(from_dir, DOCS / "js" / "site.js"),
    }


def write_html(path: Path, template_name: str, mapping: dict[str, str]) -> None:
    mapping.setdefault("TOC", "")
    mapping.setdefault("LIVE", _LIVE)
    template = (THEME / template_name).read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fill(template, mapping), encoding="utf-8")


def side_toc(toc: str) -> str:
    if toc.count("<a ") < 2:
        return ""
    return f'<nav class="article-toc" aria-label="目录">{toc}</nav>'


def tag_href(from_dir: Path, tag: str) -> str:
    return dir_href(from_dir, DOCS / "tags" / tag)


def post_href(from_dir: Path, post: Post) -> str:
    return dir_href(from_dir, DOCS / "blog" / post.stem)


def render_tags(tags: tuple[str, ...], from_dir: Path) -> str:
    if not tags:
        return ""
    links = "".join(
        f'<a class="tag" href="{html.escape(tag_href(from_dir, tag))}">{html.escape(tag)}</a>'
        for tag in tags
    )
    return f'<span class="tag-list">{links}</span>'


def article_page(date: str, body: str, kicker_href: str = "", kicker: str = "", tags: str = "") -> str:
    label = ""
    if kicker:
        label = f'<p class="kicker"><a href="{html.escape(kicker_href)}">{html.escape(kicker)}</a></p>'
    bits = []
    if date:
        bits.append(f'<time datetime="{html.escape(date)}">{html.escape(date)}</time>')
    if tags:
        bits.append(tags)
    when = f'<div class="article-meta">{"".join(bits)}</div>' if bits else ""
    return f'<article class="prose">{label}{when}{body}</article>'


def blog_index(posts: list[Post], from_dir: Path, heading: str = "博客", lede: str = "") -> str:
    intro = f'<p class="lede">{html.escape(lede)}</p>' if lede else ""
    head = f'<header class="page-head"><h1>{html.escape(heading)}</h1>{intro}</header>'
    if not posts:
        return head + '<p class="empty">还没有文章。</p>'
    items = []
    for post in posts:
        summary = f'<span class="post-excerpt">{html.escape(post.summary)}</span>' if post.summary else ""
        tags = render_tags(post.tags, from_dir)
        items.append(
            '<li class="post-item"><a class="post-body" href="'
            + html.escape(post_href(from_dir, post))
            + '"><span class="post-title">'
            + html.escape(post.title)
            + "</span>"
            + summary
            + "</a>"
            + tags
            + f'<time datetime="{html.escape(post.date)}">{html.escape(post.date)}</time></li>'
        )
    return head + '<ul class="post-list">' + "".join(items) + "</ul>"


def travel_index(trips: list[Trip], provinces: list[dict[str, str]]) -> str:
    by_id: dict[str, list[Trip]] = {}
    known = {item["id"] for item in provinces}
    for trip in trips:
        key = short_name(trip.province)
        by_id.setdefault(key, []).append(trip)
        if key not in known:
            print(f"warning: {trip.province} is not a province on the map", flush=True)
    payload = {
        item["id"]: {
            "name": item["id"],
            "articles": [{"title": trip.title, "href": trip.href_from_travel()} for trip in by_id.get(item["id"], [])],
        }
        for item in provinces
    }
    shapes = []
    for item in provinces:
        visited = " visited" if by_id.get(item["id"]) else ""
        shapes.append(
            f'<path class="province{visited}" data-id="{html.escape(item["id"], quote=True)}" '
            f'd="{item["d"]}"></path>'
        )
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    return (
        '<header class="page-head"><h1>游记</h1></header>'
        '<div class="map-frame"><svg id="china-map" viewBox="'
        + map_viewbox()
        + '" role="img" aria-label="中国地图">'
        + "".join(shapes)
        + '</svg><div class="map-legend"><span><i class="swatch visited"></i>去过</span></div></div>'
        + '<div id="map-tip" class="map-tip" hidden></div>'
        + f'<script id="travel-data" type="application/json">{data}</script>'
    )


def copy_static() -> None:
    css = DOCS / "css"
    js = DOCS / "js"
    css.mkdir(parents=True, exist_ok=True)
    js.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(THEME / "site.css", css / "site.css")
    shutil.copyfile(THEME / "site.js", js / "site.js")


def code_styles(from_dir: Path, languages: list[str]) -> str:
    return "\n  ".join(
        f'<link rel="stylesheet" href="{href_between(from_dir, DOCS / "css" / f"{language}.css")}">'
        for language in languages
    )


def build(live_port: int | None = None) -> tuple[int, int]:
    global _LIVE
    _LIVE = _LIVE_SCRIPT.replace("WS_PORT", str(live_port)) if live_port else ""
    if not CHINA.exists():
        raise SystemExit(f"missing map data: {CHINA}")
    reset_languages()
    dates = git_dates()
    posts = collect_posts()
    trips = collect_trips(dates)
    provinces = load_provinces(CHINA)
    about_html, about_mermaid = markdown_page(ROOT / "about-me.md", "还没有介绍。")

    if DOCS.exists():
        shutil.rmtree(DOCS)
    DOCS.mkdir()
    copy_static()
    languages = export_code_css(DOCS / "css")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")

    write_html(
        DOCS / "index.html",
        "layout.html",
        {
            "TITLE": f"博客 · {SITE_NAME}",
            "HEADER": render_header(DOCS, "blog"),
            "WIDE": "",
            "CONTENT": blog_index(posts, DOCS),
            "CODE_CSS": code_styles(DOCS, languages),
            "EXTRA": "",
            **asset_map(DOCS),
        },
    )

    for post in posts:
        folder = DOCS / "blog" / post.stem
        write_html(
            folder / "index.html",
            "layout.html",
            {
                "TITLE": html.escape(f"{post.title} · {SITE_NAME}"),
                "HEADER": render_header(folder, "blog"),
                "WIDE": "",
                "CONTENT": article_page(post.date, post.html, tags=render_tags(post.tags, folder)),
                "TOC": side_toc(post.toc),
                "CODE_CSS": code_styles(folder, languages),
                "EXTRA": MERMAID_SCRIPT if post.has_mermaid else "",
                **asset_map(folder),
            },
        )

    grouped: dict[str, list[Post]] = {}
    for post in posts:
        for tag in post.tags:
            grouped.setdefault(tag, []).append(post)
    for tag, tagged in grouped.items():
        folder = DOCS / "tags" / tag
        write_html(
            folder / "index.html",
            "layout.html",
            {
                "TITLE": html.escape(f"{tag} · 博客 · {SITE_NAME}"),
                "HEADER": render_header(folder, "blog"),
                "WIDE": "",
                "CONTENT": blog_index(tagged, folder, heading=tag, lede=f"{len(tagged)} 篇"),
                "CODE_CSS": code_styles(folder, languages),
                "EXTRA": "",
                **asset_map(folder),
            },
        )

    notes_html, notes_mermaid = markdown_page(ROOT / "note.md", "还没有笔记介绍。")
    notes_dir = DOCS / "notes"
    write_html(
        notes_dir / "index.html",
        "layout.html",
        {
            "TITLE": f"笔记 · {SITE_NAME}",
            "HEADER": render_header(notes_dir, "notes"),
            "WIDE": "",
            "CONTENT": notes_html,
            "CODE_CSS": code_styles(notes_dir, languages),
            "EXTRA": MERMAID_SCRIPT if notes_mermaid else "",
            **asset_map(notes_dir),
        },
    )

    tools_html, tools_mermaid = markdown_page(ROOT / "tools.md", "还没有工具。")
    tools_dir = DOCS / "tools"
    write_html(
        tools_dir / "index.html",
        "layout.html",
        {
            "TITLE": f"工具 · {SITE_NAME}",
            "HEADER": render_header(tools_dir, "tools"),
            "WIDE": "",
            "CONTENT": tools_html,
            "CODE_CSS": code_styles(tools_dir, languages),
            "EXTRA": MERMAID_SCRIPT if tools_mermaid else "",
            **asset_map(tools_dir),
        },
    )

    travel_dir = DOCS / "travel"
    write_html(
        travel_dir / "index.html",
        "layout.html",
        {
            "TITLE": f"游记 · {SITE_NAME}",
            "HEADER": render_header(travel_dir, "travel"),
            "WIDE": " wide map-page",
            "CONTENT": travel_index(trips, provinces),
            "CODE_CSS": code_styles(travel_dir, languages),
            "EXTRA": "",
            **asset_map(travel_dir),
        },
    )
    for trip in trips:
        folder = trip.out_dir()
        write_html(
            folder / "index.html",
            "layout.html",
            {
                "TITLE": html.escape(f"{trip.title} · {SITE_NAME}"),
                "HEADER": render_header(folder, "travel"),
                "WIDE": "",
                "CONTENT": article_page(trip.date, trip.html, dir_href(folder, travel_dir), "游记"),
                "CODE_CSS": code_styles(folder, languages),
                "EXTRA": MERMAID_SCRIPT if trip.has_mermaid else "",
                **asset_map(folder),
            },
        )

    about_dir = DOCS / "about"
    write_html(
        about_dir / "index.html",
        "layout.html",
        {
            "TITLE": f"本人 · {SITE_NAME}",
            "HEADER": render_header(about_dir, "about"),
            "WIDE": "",
            "CONTENT": about_html,
            "CODE_CSS": code_styles(about_dir, languages),
            "EXTRA": MERMAID_SCRIPT if about_mermaid else "",
            **asset_map(about_dir),
        },
    )
    return len(posts), len(trips)


def _pick_port(start: int) -> int:
    port = start
    while port < 65535:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    raise SystemExit("no free port for live reload")


async def _live_handler(websocket, *_args) -> None:
    _LIVE_CLIENTS.add(websocket)
    try:
        await websocket.wait_closed()
    finally:
        _LIVE_CLIENTS.discard(websocket)


async def _notify_reload() -> None:
    if not _LIVE_CLIENTS:
        return
    await asyncio.gather(*[client.send("reload") for client in list(_LIVE_CLIENTS)], return_exceptions=True)


def _notify_browsers() -> None:
    if _LIVE_LOOP is None:
        return
    asyncio.run_coroutine_threadsafe(_notify_reload(), _LIVE_LOOP).result(timeout=5)


def _serve_live_socket(port: int) -> None:
    global _LIVE_LOOP
    import websockets

    async def run() -> None:
        global _LIVE_LOOP
        _LIVE_LOOP = asyncio.get_running_loop()
        async with websockets.serve(_live_handler, "127.0.0.1", port):
            await asyncio.Future()

    asyncio.run(run())


class _BlogWatch:
    def __init__(self, live_port: int) -> None:
        from watchdog.events import FileSystemEventHandler

        self._live_port = live_port
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

        class Handler(FileSystemEventHandler):
            def on_modified(self, event):
                owner._queue(event)

            def on_created(self, event):
                owner._queue(event)

            def on_moved(self, event):
                owner._queue(event)

            def on_deleted(self, event):
                owner._queue(event)

        owner = self
        self.handler = Handler()

    def _queue(self, event) -> None:
        if event.is_directory:
            return
        path = getattr(event, "dest_path", None) or event.src_path
        name = Path(str(path)).name.lower()
        if not (name.endswith(".md") or name == "posts.yaml"):
            return
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(2.0, self._rebuild)
        self._timer.daemon = True
        self._timer.start()

    def _rebuild(self) -> None:
        with self._lock:
            self._timer = None
            print("正在刷新...", flush=True)
            try:
                blogs, trips = build(self._live_port)
            except Exception as exc:
                print(f"刷新失败: {exc}", flush=True)
                return
            print(f"blog {blogs}, travel {trips} -> docs/", flush=True)
        _notify_browsers()


def _watch_blog(live_port: int):
    from watchdog.observers import Observer

    blog = ROOT / "blog"
    blog.mkdir(parents=True, exist_ok=True)
    watch = _BlogWatch(live_port)
    observer = Observer()
    observer.schedule(watch.handler, str(blog), recursive=True)
    observer.start()
    return observer


def serve(port: int, open_browser: bool, live_port: int | None = None) -> None:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(DOCS), **kwargs)

        def log_message(self, format, *args):
            return

    httpd = None
    for candidate in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit(f"no free port from {port}")

    observer = None
    if live_port:
        threading.Thread(target=_serve_live_socket, args=(live_port,), daemon=True).start()
        observer = _watch_blog(live_port)

    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(url, flush=True)
    if live_port:
        print("watching blog/", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        if observer is not None:
            observer.stop()
            observer.join(timeout=2)
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the static site and serve docs/")
    parser.add_argument("--build-only", action="store_true", help="write docs/ without starting the server")
    parser.add_argument("--port", type=int, default=9381)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args()
    live_port = None if args.build_only else _pick_port(8765)
    blogs, trips = build(live_port)
    print(f"blog {blogs}, travel {trips} -> docs/", flush=True)
    if not args.build_only:
        serve(args.port, not args.no_open, live_port)


if __name__ == "__main__":
    main()
