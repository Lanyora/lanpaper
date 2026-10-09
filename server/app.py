#!/usr/bin/env python3
"""局域网轻量记事本 —— 服务入口。

用法：
    python3 server/app.py                # 默认读取 config.json 的端口
    python3 server/app.py --port 9000    # 临时换端口
    python3 server/app.py --no-browser   # 不自动打开浏览器
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# 让 `python3 server/app.py` 与 `python3 -m server.app` 两种跑法都成立
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import auth, db as db_mod, handlers, util  # noqa: E402
from server.util import ApiError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.json"

DEFAULT_CONFIG = {
    "app_title": "局域网轻量记事本",
    "port": 8000,
    "bind_host": "0.0.0.0",
    "initial_admin_code": "",
    "session_hours": 4,
    "max_upload_mb": 5,
    "allowed_image_ext": ["png", "jpg", "jpeg", "gif", "webp"],
    "page_size": 20,
    "open_browser": True,
}

MEDIA_RE = re.compile(r"^/media/([0-9a-f]{32}\.(?:png|jpg|jpeg|gif|webp))$")
MAX_BODY_BYTES = 64 * 1024 * 1024  # 硬上限，防止超大请求把内存吃光

ROUTES = [
    ("GET", r"^/api/bootstrap$", handlers.h_bootstrap),
    ("POST", r"^/api/register$", handlers.h_register),
    ("POST", r"^/api/login$", handlers.h_login),
    ("POST", r"^/api/logout$", handlers.h_logout),
    ("GET", r"^/api/me$", handlers.h_me_get),
    ("PATCH", r"^/api/me$", handlers.h_me_patch),
    ("POST", r"^/api/invites$", handlers.h_invite_create),
    ("GET", r"^/api/invites$", handlers.h_invite_list),

    ("GET", r"^/api/items$", handlers.h_items_list),
    ("POST", r"^/api/items$", handlers.h_items_create),
    ("GET", r"^/api/items/(?P<item_id>\d+)$", handlers.h_item_get),
    ("DELETE", r"^/api/items/(?P<item_id>\d+)$", handlers.h_item_delete),
    ("POST", r"^/api/items/(?P<item_id>\d+)/progress$", handlers.h_progress_create),
    ("POST", r"^/api/items/(?P<item_id>\d+)/advance$", handlers.h_item_advance),
    ("POST", r"^/api/items/(?P<item_id>\d+)/rollback$", handlers.h_item_rollback),
    ("POST", r"^/api/items/(?P<item_id>\d+)/reset$", handlers.h_item_reset),
    ("GET", r"^/api/items/(?P<item_id>\d+)/events$", handlers.h_events_list),
    ("POST", r"^/api/items/(?P<item_id>\d+)/events$", handlers.h_events_create),

    ("PATCH", r"^/api/progress/(?P<pid>\d+)$", handlers.h_progress_patch),
    ("DELETE", r"^/api/progress/(?P<pid>\d+)$", handlers.h_progress_delete),

    ("GET", r"^/api/feed$", handlers.h_feed),
    ("DELETE", r"^/api/events/(?P<eid>\d+)$", handlers.h_event_delete),
    ("GET", r"^/api/todos$", handlers.h_todos),

    ("GET", r"^/api/checklist$", handlers.h_checklist),
    ("POST", r"^/api/checklist$", handlers.h_checklist_create),
    ("PATCH", r"^/api/checklist/(?P<tid>\d+)$", handlers.h_checklist_patch),
    ("DELETE", r"^/api/checklist/(?P<tid>\d+)$", handlers.h_checklist_delete),

    ("GET", r"^/api/members$", handlers.h_members),

    ("GET", r"^/api/templates$", handlers.h_templates_list),
    ("POST", r"^/api/templates$", handlers.h_template_create),
    ("GET", r"^/api/templates/(?P<tid>\d+)$", handlers.h_template_get),
    ("DELETE", r"^/api/templates/(?P<tid>\d+)$", handlers.h_template_delete),

    ("GET", r"^/api/recurring$", handlers.h_recurring_list),
    ("POST", r"^/api/recurring$", handlers.h_recurring_create),
    ("PATCH", r"^/api/recurring/(?P<rid>\d+)$", handlers.h_recurring_patch),
    ("DELETE", r"^/api/recurring/(?P<rid>\d+)$", handlers.h_recurring_delete),

    ("GET", r"^/api/tags$", handlers.h_tags),
    ("GET", r"^/api/search$", handlers.h_search),
    ("GET", r"^/api/stats$", handlers.h_stats),
    ("POST", r"^/api/upload$", handlers.h_upload),
]
COMPILED_ROUTES = [(m, re.compile(p), f) for m, p, f in ROUTES]


# ================================================================ 配置


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                # 下划线开头的是给人看的注释，跳过
                cfg.update({k: v for k, v in raw.items() if not k.startswith("_")})
        except (ValueError, OSError) as exc:
            print(f"⚠️  config.json 读取失败，改用默认配置：{exc}")
    return cfg


# ================================================================ 服务器状态


class ServerState:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.data_dir = ROOT / "data"
        self.images_dir = self.data_dir / "images"
        self.logs_dir = self.data_dir / "logs"
        self.web_dir = ROOT / "web"
        for folder in (self.data_dir, self.images_dir, self.logs_dir):
            folder.mkdir(parents=True, exist_ok=True)

        self.db = db_mod.DB(self.data_dir / "notes.db")
        self.db.init_schema()

        # 初始管理员邀请码：配置里填了就用，留空则随机生成并打印
        configured = (cfg.get("initial_admin_code") or "").strip()
        if configured:
            self.initial_admin_code = auth.normalize_code(configured)
            self.admin_code_from_config = True
        else:
            self.initial_admin_code = auth.new_invite_code()
            self.admin_code_from_config = False

        self.port = None
        # 本机的局域网地址。所有"来自回环地址"的请求都折算成它，
        # 这样同一台机器无论用 127.0.0.1 还是 192.168.x.x 访问，身份都一致。
        self.local_ip = util.lan_ips()[0]
        self._log_lock = threading.Lock()

    # ------------------------------------------------------------ 工具

    def log(self, text: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {text}"
        with self._log_lock:
            try:
                with (self.logs_dir / "app.log").open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    def has_users(self) -> bool:
        return (self.db.scalar("SELECT COUNT(*) FROM users") or 0) > 0


# ================================================================ 定时规则


def create_recurring_todo(state: ServerState, rule) -> int | None:
    """按规则生成一条待办。

    日常点检表、Audit 这类东西的生命周期只有"今天做没做"，
    塞进任务列表会污染主线内容，所以它落在 todos 表里，界面上是一个能打勾的清单。
    同一条规则同一天只生成一次。
    """
    db = state.db
    author = db.queryone("SELECT id FROM users WHERE id = ?", (rule["created_by"],))
    if author is None:
        return None

    today = util.today_key()
    if db.queryone(
        "SELECT id FROM todos WHERE rule_id = ? AND due_date = ?", (rule["id"], today)
    ):
        return None

    cur = db.execute(
        """INSERT INTO todos (title, created_by, due_date, source, rule_id, created_at)
           VALUES (?, ?, ?, 'recurring', ?, ?)""",
        (rule["title"], author["id"], today, rule["id"], util.now_iso()),
    )
    return cur.lastrowid


def rule_matches_today(rule, weekday: int) -> bool:
    if rule["freq"] != "weekly":
        return True
    raw = rule["weekdays"] or ("" if rule["weekday"] is None else str(rule["weekday"]))
    days = {int(x) for x in raw.split(",") if x != ""}
    return weekday in days


def run_recurring_once(state: ServerState) -> int:
    """到点就生成，同一天不重复。返回本次生成的数量。"""
    db = state.db
    now = util.now_dt()
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    weekday = now.weekday()  # 0 = 周一

    made = 0
    for rule in db.query("SELECT * FROM recurring_rules WHERE enabled = 1"):
        if rule["time_of_day"] > hhmm:
            continue
        if rule["last_run_at"] and rule["last_run_at"][:10] == today:
            continue
        if not rule_matches_today(rule, weekday):
            continue
        try:
            if create_recurring_todo(state, rule) is not None:
                made += 1
                db.execute(
                    "UPDATE recurring_rules SET last_run_at = ? WHERE id = ?",
                    (util.now_iso(), rule["id"]),
                )
                state.log(f"定时规则「{rule['title']}」已生成待办")
        except Exception as exc:  # 单条规则失败不影响其他
            state.log(f"定时规则 {rule['id']} 生成失败：{exc}")
    return made


def recurring_loop(state: ServerState, stop_event: threading.Event) -> None:
    while not stop_event.wait(30):
        try:
            run_recurring_once(state)
        except Exception as exc:
            state.log(f"定时线程异常：{exc}")


# ================================================================ HTTP


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler_cls, state: ServerState):
        super().__init__(addr, handler_cls)
        self.state = state

    def handle_error(self, request, client_address):
        """客户端提前断开是常态（刷新页面、关标签页），不该刷一屏 traceback。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        # 其它异常仍打到 stderr，方便真出问题时看得见
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "LanyoraNotes"
    protocol_version = "HTTP/1.1"

    # -------------------------------------------------------- 日志

    def log_message(self, fmt, *args):  # 默认会往 stderr 刷屏，改为写文件
        self.server.state.log(f"{self.address_string()} {fmt % args}")

    # -------------------------------------------------------- 方法

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    # -------------------------------------------------------- 请求解析

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "too_large", "请求内容过大")
        return self.rfile.read(length)

    def _session(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("sid")
        if not morsel:
            return None
        return auth.resolve_session(self.server.state.db, morsel.value)

    # -------------------------------------------------------- 分发

    def dispatch(self, method: str) -> None:
        state = self.server.state
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        try:
            if not path.startswith("/api/"):
                if method != "GET":
                    return self._json(405, {"error": {"code": "method_not_allowed",
                                                      "message": "方法不被允许"}})
                return self._serve_static(path)

            raw = self._read_body()
            body = {}
            if raw:
                ctype = self.headers.get("Content-Type", "")
                if ctype.startswith("application/json"):
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        raise ApiError(400, "bad_json", "请求内容格式不正确")
                elif ctype.startswith("multipart/form-data"):
                    body = {}
                else:
                    body = {}

            session = self._session()
            ip = util.normalize_ip(self.client_address[0], state.local_ip)
            if session is not None:
                auth.touch_session(state.db, session["session_id"], ip)

            for route_method, pattern, func in COMPILED_ROUTES:
                match = pattern.match(path)
                if not match:
                    continue
                if route_method != method:
                    continue
                ctx = handlers.Ctx(state, self, method, path, query, body, session, ip, raw)
                status, payload = func(ctx, **match.groupdict())
                return self._json(status, payload, ctx.headers)

            return self._json(404, {"error": {"code": "no_route",
                                              "message": f"接口不存在：{method} {path}"}})

        except ApiError as exc:
            return self._json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except BrokenPipeError:
            return
        except Exception:
            state.log("未捕获异常：\n" + traceback.format_exc())
            return self._json(500, {"error": {"code": "server_error",
                                              "message": "服务器内部错误，详见 data/logs/app.log"}})

    # -------------------------------------------------------- 响应

    def _json(self, status: int, payload, headers: dict | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -------------------------------------------------------- 静态资源

    def _serve_static(self, path: str) -> None:
        state = self.server.state

        media = MEDIA_RE.match(path)
        if media:
            return self._send_file(state.images_dir / media.group(1), cache=True)

        if path in ("/", "/index.html"):
            return self._send_file(state.web_dir / "index.html")

        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (state.web_dir / rel).resolve()
            base = state.web_dir.resolve()
            # 路径穿越校验：必须仍在 web/ 目录内
            if base not in target.parents and target != base:
                return self._json(403, {"error": {"code": "forbidden", "message": "非法路径"}})
            return self._send_file(target)

        # 未知路径一律回落到首页，便于前端做 hash 路由
        return self._send_file(state.web_dir / "index.html")

    def _send_file(self, fp: Path, cache: bool = False) -> None:
        if not fp.is_file():
            return self._json(404, {"error": {"code": "not_found", "message": "文件不存在"}})
        import mimetypes

        ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        # 文本类资源必须带 charset，否则浏览器可能按本地编码猜，中文会变乱码
        if (ctype.startswith("text/")
                or ctype in ("application/javascript", "application/json", "image/svg+xml")):
            ctype += "; charset=utf-8"
        data = fp.read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header(
                "Cache-Control",
                "public, max-age=31536000, immutable" if cache else "no-cache",
            )
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


# ================================================================ 启动


def print_banner(state: ServerState, port: int) -> None:
    cfg = state.cfg
    line = "─" * 52
    print()
    print(f"  {cfg['app_title']}  已启动")
    print(f"  {line}")
    print(f"  本机访问        http://127.0.0.1:{port}")
    for ip in util.lan_ips():
        print(f"  局域网访问      http://{ip}:{port}")
    print(f"  数据目录        {state.data_dir}")
    print(f"  日志            {state.logs_dir / 'app.log'}")

    if not state.has_users():
        print(f"  {line}")
        if state.admin_code_from_config:
            print("  初始管理员邀请码  来自 config.json（请自行查看该文件）")
        else:
            print(f"  初始管理员邀请码  {auth.display_code(state.initial_admin_code)}")
        print("  （首次注册用，注册成功后此码即失效）")
    print(f"  {line}")
    if port != cfg["port"]:
        print(f"  注意：config.json 里配置的 {cfg['port']} 端口被占用，已自动改用 {port}")
    print("  按 Ctrl+C 停止服务")
    print()
    sys.stdout.flush()  # 输出被重定向到管道/文件时也能立刻看到


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="局域网轻量记事本")
    parser.add_argument("--port", type=int, default=None, help="覆盖 config.json 中的端口")
    parser.add_argument("--host", default=None, help="覆盖监听地址")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.port:
        cfg["port"] = args.port
    if args.host:
        cfg["bind_host"] = args.host

    state = ServerState(cfg)
    purged = auth.purge_expired_sessions(state.db)
    if purged:
        state.log(f"清理过期会话 {purged} 条")
    # 访问明细只用于阅读量去重，保留 7 天足够
    state.db.execute("DELETE FROM view_logs WHERE viewed_at < ?", (util.iso_days_ago(7),))

    # 端口被占用时依次向后探测
    host = cfg["bind_host"]
    httpd = None
    port = int(cfg["port"])
    for candidate in range(port, port + 20):
        try:
            httpd = AppServer((host, candidate), Handler, state)
            port = candidate
            break
        except OSError:
            continue
    if httpd is None:
        print(f"❌ {port}~{port + 19} 端口全部被占用，请修改 config.json 里的 port")
        return 1

    state.port = port
    print_banner(state, port)

    stop_event = threading.Event()
    threading.Thread(
        target=recurring_loop, args=(state, stop_event), daemon=True
    ).start()

    if cfg.get("open_browser", True) and not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  正在停止服务…")
    finally:
        stop_event.set()
        httpd.shutdown()
        httpd.server_close()
        state.db.close()
        print("  已停止。数据保存在 data/ 目录，随时可以再启动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
