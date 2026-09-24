#!/usr/bin/env python3
"""本地待办事项服务。

    python todo/server.py            # http://127.0.0.1:9521
    python todo/server.py --test-mail  # 立即发送一次未完成事项邮件

每天北京时间 18:00 若有未完成事项，通过 e2me 发送邮件给自己。
e2me 凭据读取顺序：环境变量 E2ME_EMAIL/E2ME_PASSWD > todo/e2me.toml > e2me 全局配置。
"""

import argparse
import contextlib
import io
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "todos.json"
STATE_FILE = BASE_DIR / "state.json"
INDEX_FILE = BASE_DIR / "index.html"
E2ME_CONFIG = BASE_DIR / "e2me.toml"

HOST = "127.0.0.1"
PORT = 9521
TZ = ZoneInfo("Asia/Shanghai")
NOTIFY_HOUR = 18

lock = threading.Lock()
mail_lock = threading.Lock()


def now_bj() -> datetime:
    return datetime.now(TZ)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def load_todos() -> list:
    return load_json(DATA_FILE, [])


def save_todos(todos: list) -> None:
    save_json(DATA_FILE, todos)


# ---------------------------------------------------------------- mail

def send_pending_mail() -> tuple[bool, str]:
    with lock:
        pending = [t for t in load_todos() if not t.get("done")]
    pending.sort(key=lambda t: not t.get("starred"))
    if not pending:
        return False, "没有未完成的待办事项"

    lines = [f"截至 {now_bj():%Y-%m-%d %H:%M}，你还有 {len(pending)} 项待办未完成：", ""]
    for i, t in enumerate(pending, 1):
        lines.append(f"{i}. {'★ ' if t.get('starred') else ''}{t['title']}")
        if t.get("note"):
            lines.append(f"   {t['note']}")
    lines += ["", f"管理待办: http://{HOST}:{PORT}"]

    try:
        import e2me
    except ImportError:
        return False, "发送失败: 未安装 e2me (pip install e2me)"
    err = e2me_config_error()
    if err:
        return False, f"发送失败: {err}"
    out = io.StringIO()
    try:
        # e2me 吞掉 SMTP 异常只打印日志，遇到不支持的邮箱会直接 exit(1)
        with mail_lock, contextlib.redirect_stdout(out):
            e2me.send_email(
                subject=f"[Todo] {len(pending)} 项待办未完成",
                body="\n".join(lines),
                config_path=str(E2ME_CONFIG),
            )
    except (Exception, SystemExit) as e:  # noqa: BLE001
        return False, f"发送失败: {e!r}"
    log = out.getvalue()
    if "Email sent successfully" not in log:
        last = log.strip().splitlines()[-1] if log.strip() else "未知错误"
        return False, f"发送失败: {last}"
    return True, f"已发送 {len(pending)} 项未完成待办"


E2ME_DOMAINS = ("163", "qq", "gmail")


def e2me_config_error() -> str | None:
    import e2me
    import toml

    path = E2ME_CONFIG if E2ME_CONFIG.exists() else Path(e2me.__file__).parent / "e2me.toml"
    try:
        email = toml.load(path).get("email", {})
    except Exception:  # noqa: BLE001
        email = {}
    addr = os.getenv("E2ME_EMAIL") or email.get("email", "")
    passwd = os.getenv("E2ME_PASSWD") or email.get("passwd", "")
    if not addr or addr == "your-email@example.com" or not passwd or passwd == "xxx":
        return "e2me 未配置邮箱或 SMTP 授权码，见 todo/e2me.toml.example"
    domain = addr.rpartition("@")[2].split(".")[0]
    if domain not in E2ME_DOMAINS:
        return f"e2me 仅支持 {'/'.join(E2ME_DOMAINS)} 邮箱，当前为 {addr}"
    return None


def scheduler() -> None:
    # 轮询而非长 sleep，电脑休眠唤醒后也能补发当天的提醒
    while True:
        now = now_bj()
        today = now.date().isoformat()
        state = load_json(STATE_FILE, {})
        if now.hour >= NOTIFY_HOUR and state.get("last_notified") != today:
            try:
                ok, msg = send_pending_mail()
            except Exception as e:  # noqa: BLE001
                ok, msg = False, f"发送失败: {e!r}"
            print(f"[{now:%F %T}] daily notify: {msg}", flush=True)
            if ok or msg == "没有未完成的待办事项":
                state["last_notified"] = today
                save_json(STATE_FILE, state)
            else:
                time.sleep(600)
                continue
        time.sleep(30)


# ---------------------------------------------------------------- http

TODO_PATH = re.compile(r"^/api/todos/([\w-]+)$")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = INDEX_FILE.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/todos":
            with lock:
                self.send_json(load_todos())
        elif self.path == "/api/status":
            state = load_json(STATE_FILE, {})
            self.send_json({"last_notified": state.get("last_notified"), "notify_hour": NOTIFY_HOUR})
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if self.path == "/api/todos":
            data = self.read_json()
            title = str(data.get("title", "")).strip()
            if not title:
                return self.send_json({"error": "标题不能为空"}, HTTPStatus.BAD_REQUEST)
            ts = now_bj().isoformat(timespec="seconds")
            todo = {
                "id": uuid.uuid4().hex[:12],
                "title": title,
                "note": str(data.get("note", "")).strip(),
                "done": False,
                "starred": bool(data.get("starred")),
                "created_at": ts,
                "updated_at": ts,
            }
            with lock:
                todos = load_todos()
                todos.insert(0, todo)
                save_todos(todos)
            self.send_json(todo, HTTPStatus.CREATED)
        elif self.path == "/api/notify":
            ok, msg = send_pending_mail()
            self.send_json({"ok": ok, "message": msg})
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_PATCH(self):
        m = TODO_PATH.match(self.path)
        if not m:
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        data = self.read_json()
        with lock:
            todos = load_todos()
            todo = next((t for t in todos if t["id"] == m.group(1)), None)
            if todo is None:
                return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            if "title" in data:
                title = str(data["title"]).strip()
                if not title:
                    return self.send_json({"error": "标题不能为空"}, HTTPStatus.BAD_REQUEST)
                todo["title"] = title
            if "note" in data:
                todo["note"] = str(data["note"]).strip()
            if "done" in data:
                todo["done"] = bool(data["done"])
            if "starred" in data:
                todo["starred"] = bool(data["starred"])
            todo["updated_at"] = now_bj().isoformat(timespec="seconds")
            save_todos(todos)
        self.send_json(todo)

    def do_DELETE(self):
        m = TODO_PATH.match(self.path)
        if not m:
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        with lock:
            todos = load_todos()
            remaining = [t for t in todos if t["id"] != m.group(1)]
            if len(remaining) == len(todos):
                return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            save_todos(remaining)
        self.send_json({"ok": True})


def main():
    parser = argparse.ArgumentParser(description="本地待办事项服务")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--test-mail", action="store_true", help="立即发送一次未完成事项邮件后退出")
    args = parser.parse_args()

    if args.test_mail:
        print(send_pending_mail()[1])
        return

    threading.Thread(target=scheduler, daemon=True).start()
    server = ThreadingHTTPServer((HOST, args.port), Handler)
    print(f"Todo server running at http://{HOST}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
