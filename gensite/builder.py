"""Build the static site in docs/ from blog posts, note.md, tools.md, and travel notes."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
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


def load_blog_times(path: Path) -> dict[str, str]:
    times: dict[str, str] = {}
    if not path.exists():
        return times
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r'^(?:"([^"]+)"|(\S+))\s*:\s*(\d{4}-\d{2}-\d{2})\s*$', line)
        if match:
            times[match.group(1) or match.group(2)] = match.group(3)
    return times


def ensure_blog_times(files: list[Path]) -> dict[str, str]:
    path = ROOT / "blog" / "time.yaml"
    times = load_blog_times(path)
    missing = [item.name for item in files if item.name not in times]
    if not missing and path.exists():
        return times
    today = datetime.now().strftime("%Y-%m-%d")
    for name in missing:
        times[name] = today
    lines = ["# 每篇博客的创建时间。已有日期不会在构建时改写，新文章会补上当天日期。", ""]
    for name in sorted(times):
        lines.append(f'"{name}": {times[name]}')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return times


@dataclass
class Post:
    title: str
    stem: str
    date: str
    summary: str
    html: str
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
    times = ensure_blog_times(files)
    used: set[str] = set()
    posts: list[Post] = []
    for path in files:
        stem = path.stem
        if stem in used:
            stem = path.relative_to(blog).with_suffix("").as_posix().replace("/", "-")
        used.add(stem)
        text = path.read_text(encoding="utf-8")
        doc = parse_markdown(text, str(path))
        posts.append(
            Post(
                title=doc.title or stem,
                stem=stem,
                date=times.get(path.name) or file_date(path, {}),
                summary=excerpt(text),
                html=doc.html,
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
    template = (THEME / template_name).read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fill(template, mapping), encoding="utf-8")


def article_page(date: str, body: str, kicker_href: str = "", kicker: str = "") -> str:
    label = ""
    if kicker:
        label = f'<p class="kicker"><a href="{html.escape(kicker_href)}">{html.escape(kicker)}</a></p>'
    when = f'<p class="meta"><time datetime="{date}">{date}</time></p>' if date else ""
    return f'<article class="prose">{label}{when}{body}</article>'


def blog_index(posts: list[Post]) -> str:
    if not posts:
        return '<header class="page-head"><h1>博客</h1></header><p class="empty">还没有文章。</p>'
    items = []
    for post in posts:
        href = encode_rel(f"blog/{post.stem}/")
        summary = f'<span class="post-excerpt">{html.escape(post.summary)}</span>' if post.summary else ""
        items.append(
            "<li><a href=\""
            + html.escape(href)
            + '"><span class="post-title">'
            + html.escape(post.title)
            + "</span>"
            + summary
            + f'<time datetime="{post.date}">{post.date}</time></a></li>'
        )
    return '<header class="page-head"><h1>博客</h1></header><ul class="post-list">' + "".join(items) + "</ul>"


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
    rows = []
    for trip in trips:
        summary = f'<span class="trip-title">{html.escape(trip.summary or trip.title)}</span>'
        rows.append(
            f'<li><a href="{html.escape(trip.href_from_travel())}">'
            f'<span class="trip-place">{html.escape(trip.place)}</span>{summary}'
            f'<time datetime="{trip.date}">{trip.date}</time></a></li>'
        )
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    listing = '<ul class="trip-list">' + "".join(rows) + "</ul>" if rows else '<p class="empty">还没有游记。</p>'
    return (
        '<header class="page-head"><h1>游记</h1></header>'
        '<div class="map-frame"><svg id="china-map" viewBox="'
        + map_viewbox()
        + '" role="img" aria-label="中国地图">'
        + "".join(shapes)
        + '</svg><div class="map-legend"><span><i class="swatch visited"></i>去过</span></div></div>'
        + '<div id="map-tip" class="map-tip" hidden></div>'
        + listing
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


def build() -> tuple[int, int]:
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
            "CONTENT": blog_index(posts),
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
                "CONTENT": article_page(post.date, post.html),
                "CODE_CSS": code_styles(folder, languages),
                "EXTRA": MERMAID_SCRIPT if post.has_mermaid else "",
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
            "WIDE": " wide",
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


def serve(port: int, open_browser: bool) -> None:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(DOCS), **kwargs)

    httpd = None
    for candidate in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit(f"no free port from {port}")
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(url, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the static site and serve docs/")
    parser.add_argument("--build-only", action="store_true", help="write docs/ without starting the server")
    parser.add_argument("--port", type=int, default=9381)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args()
    blogs, trips = build()
    print(f"blog {blogs}, travel {trips} -> docs/", flush=True)
    if not args.build_only:
        serve(args.port, not args.no_open)


if __name__ == "__main__":
    main()
