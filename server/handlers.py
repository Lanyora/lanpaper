"""所有 API 处理函数。

每个处理函数签名统一为 handler(ctx, **path_params) -> (status, payload)
需要额外响应头时写进 ctx.headers。
"""

from __future__ import annotations

import hashlib
import json
import re

from server import auth, util
from server.util import ApiError, bad_request, conflict, forbidden, not_found, unauthorized

# ---------------------------------------------------------------- 上下文


class Ctx:
    def __init__(self, server, handler, method, path, query, body, session, ip,
                 raw_body: bytes = b""):
        self.server = server
        self.handler = handler
        self.method = method
        self.path = path
        self.query = query
        self.body = body if isinstance(body, dict) else {}
        self.session = session
        self.user = session
        self.ip = ip
        self.raw_body = raw_body
        self.headers: dict[str, str] = {}

    @property
    def db(self):
        return self.server.db

    @property
    def cfg(self):
        return self.server.cfg

    def login(self):
        if self.user is None:
            raise unauthorized()
        return self.user

    def admin(self):
        self.login()
        if not self.user["is_admin"]:
            raise forbidden("需要管理员权限")
        return self.user

    def cookie(self, value: str, max_age: int) -> None:
        self.headers["Set-Cookie"] = (
            f"sid={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
        )


# ---------------------------------------------------------------- 小工具


def _col(row, name, default=None):
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def _avatar_url(avatar_hash, ext):
    if not avatar_hash:
        return None
    return f"/media/{avatar_hash}.{ext or 'png'}"


def _user_block(row, kind):
    """kind 为 author 或 actor，需查询里已取好对应别名列。"""
    return {
        "username": _col(row, f"{kind}_username"),
        "nickname": _col(row, f"{kind}_nickname"),
        "avatar_url": _avatar_url(
            _col(row, f"{kind}_avatar"), _col(row, f"{kind}_avatar_ext")
        ),
    }


USER_COLS = """
    u.id            AS {p}_id,
    u.username      AS {p}_username,
    u.nickname      AS {p}_nickname,
    u.avatar_hash   AS {p}_avatar,
    au.ext          AS {p}_avatar_ext
"""


def _json_list(raw, fallback=None):
    try:
        val = json.loads(raw or "[]")
        return val if isinstance(val, list) else (fallback or [])
    except (ValueError, TypeError):
        return fallback or []


def _json_dict(raw):
    try:
        val = json.loads(raw or "{}")
        return val if isinstance(val, dict) else {}
    except (ValueError, TypeError):
        return {}


def progress_public(row) -> dict:
    total = row["total"] or 1
    cursor = row["cursor"] or 0
    if total <= 1:
        percent = 100 if (cursor >= 1 or row["status"] == "done") else 0
    else:
        percent = max(0, min(100, int(cursor * 100 / total)))
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "seq": row["seq"],
        "title": row["title"],
        "status": row["status"],
        "status_label": util.status_label(row["status"]),
        "status_hint": util.status_hint(row["status"]),
        "cursor": cursor,
        "total": total,
        "percent": percent,
        "note": row["note"],
        "done_at": row["done_at"],
    }


def event_public(row) -> dict:
    payload = _json_dict(row["payload"])
    etype = row["type"]
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "todo_id": row["todo_id"],
        "progress_id": row["progress_id"],
        "type": etype,
        "type_label": util.event_type_label(etype),
        "text": util.event_text(etype, payload, row["message"]),
        # note 的正文单独放 body：一行描述是"给某阶段加了备注"，正文才是备注内容
        "body": payload.get("text", "") if etype == "note" else "",
        "actor": _user_block(row, "actor"),
        "actor_ip": row["actor_ip"],
        "payload": payload,
        "created_at": row["created_at"],
    }


def write_event(db, item_id, progress_id, user, ip, etype, message="", payload=None) -> int:
    now = util.now_iso()
    cur = db.execute(
        """INSERT INTO events
           (item_id, progress_id, actor_id, actor_ip, type, message, payload, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id,
            progress_id,
            user["id"],
            ip,
            etype,
            message,
            json.dumps(payload or {}, ensure_ascii=False),
            now,
        ),
    )
    # 任何动作都刷新"最后活动时间"——列表按它排序，最近动过的自然排在最前
    db.execute("UPDATE items SET last_activity_at = ? WHERE id = ?", (now, item_id))
    return cur.lastrowid


def write_todo_event(db, todo_id: int, user, ip: str, etype: str, title: str) -> int:
    """待办变更也进同一条事件流。

    待办不建任务，但新建 / 完成 / 取消 / 删除同样是"谁在什么时候做了什么"，
    不该因为它的载体不是任务就丢掉留痕。
    """
    payload = {"title": title}
    cur = db.execute(
        """INSERT INTO events
           (item_id, todo_id, actor_id, actor_ip, type, message, payload, created_at)
           VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)""",
        (todo_id, user["id"], ip, etype, util.describe(etype, payload),
         json.dumps(payload, ensure_ascii=False), util.now_iso()),
    )
    return cur.lastrowid


def ensure_tags(db, names) -> list[int]:
    ids = []
    for raw in names or []:
        name = (raw if isinstance(raw, str) else str(raw)).strip()
        if not name:
            continue
        name = name[:32]
        row = db.queryone("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,))
        if row:
            ids.append(row["id"])
        else:
            cur = db.execute(
                "INSERT INTO tags (name, created_at) VALUES (?, ?)",
                (name, util.now_iso()),
            )
            ids.append(cur.lastrowid)
    return ids


def _brief_rows(db, ids: list[int]) -> dict:
    """一次性取出这批内容的标签、进度汇总与最新事件，避免 N+1。"""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))

    tags: dict[int, list[str]] = {}
    for r in db.query(
        f"""SELECT it.item_id, t.name FROM item_tags it
            JOIN tags t ON t.id = it.tag_id
            WHERE it.item_id IN ({marks}) ORDER BY t.name""",
        ids,
    ):
        tags.setdefault(r["item_id"], []).append(r["name"])

    prog: dict[int, dict] = {}
    for r in db.query(
        f"""SELECT item_id, COUNT(*) AS total,
                   SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done
            FROM progress_items WHERE item_id IN ({marks}) GROUP BY item_id""",
        ids,
    ):
        total = r["total"] or 0
        done = r["done"] or 0
        prog[r["item_id"]] = {
            "done": done,
            "total": total,
            "percent": int(done * 100 / total) if total else 0,
        }

    latest: dict[int, dict] = {}
    for r in db.query(
        f"""SELECT e.*, {USER_COLS.format(p='actor')}
            FROM events e
            JOIN users u ON u.id = e.actor_id
            LEFT JOIN attachments au ON au.hash = u.avatar_hash
            WHERE e.id IN (SELECT MAX(id) FROM events WHERE item_id IN ({marks}) GROUP BY item_id)""",
        ids,
    ):
        latest[r["item_id"]] = event_public(r)

    # 每条任务里第一个未完成的进度项 —— 供列表外一键 "+1" 用
    nxt: dict[int, dict] = {}
    for r in db.query(
        f"""SELECT * FROM progress_items WHERE item_id IN ({marks})
            ORDER BY item_id, seq""",
        ids,
    ):
        if r["item_id"] not in nxt and r["status"] != "done":
            nxt[r["item_id"]] = progress_public(r)

    return {"tags": tags, "progress": prog, "latest": latest, "next": nxt}


def item_brief(row, extras: dict) -> dict:
    iid = row["id"]
    return {
        "id": iid,
        "kind": row["kind"],
        "kind_label": util.kind_label(row["kind"]),
        "title": row["title"],
        # 只给截断后的摘要，正文仍然不出现在列表里
        "excerpt": row["excerpt"] or "",
        "author": _user_block(row, "author"),
        "tags": extras["tags"].get(iid, []),
        "created_at": row["created_at"],
        "last_activity_at": row["last_activity_at"] or row["created_at"],
        "end_at": row["end_at"],
        "view_count": row["view_count"],
        "progress": extras["progress"].get(iid, {"done": 0, "total": 0, "percent": 0}),
        "next_progress": extras["next"].get(iid),
        "latest_event": extras["latest"].get(iid),
    }


ITEM_SELECT = f"""
    SELECT i.id, i.kind, i.title, i.created_at, i.last_activity_at, i.end_at, i.view_count,
           substr(i.body_md, 1, 120) AS excerpt,
           {USER_COLS.format(p='author')}
    FROM items i
    JOIN users u ON u.id = i.author_id
    LEFT JOIN attachments au ON au.hash = u.avatar_hash
"""

# 详情专用：比列表多一个 body_md。列表接口绝不能带上它，否则首页会随正文增长变慢。
ITEM_DETAIL_SELECT = f"""
    SELECT i.id, i.kind, i.title, i.body_md, i.created_at, i.last_activity_at,
           i.end_at, i.view_count,
           {USER_COLS.format(p='author')}
    FROM items i
    JOIN users u ON u.id = i.author_id
    LEFT JOIN attachments au ON au.hash = u.avatar_hash
"""


def _fetch_item(db, item_id: int, include_deleted=False):
    row = db.queryone(
        ITEM_SELECT + " WHERE i.id = ?" + ("" if include_deleted else " AND i.deleted_at IS NULL"),
        (item_id,),
    )
    if row is None:
        raise not_found("内容不存在")
    return row


def _fetch_item_detail(db, item_id: int):
    row = db.queryone(ITEM_DETAIL_SELECT + " WHERE i.id = ? AND i.deleted_at IS NULL", (item_id,))
    if row is None:
        raise not_found("内容不存在")
    return row


def _page_args(ctx):
    try:
        page = max(1, int(ctx.query.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(ctx.query.get("size", ctx.cfg["page_size"]))
    except (TypeError, ValueError):
        size = ctx.cfg["page_size"]
    size = max(1, min(200, size))
    return page, size, (page - 1) * size


# ================================================================ 鉴权


def h_bootstrap(ctx):
    count = ctx.db.scalar("SELECT COUNT(*) FROM users") or 0
    return 200, {
        "app_title": ctx.cfg["app_title"],
        "needs_setup": count == 0,
        "logged_in": ctx.user is not None,
        "session_hours": ctx.cfg["session_hours"],
        "max_upload_mb": ctx.cfg["max_upload_mb"],
        "allowed_image_ext": ctx.cfg["allowed_image_ext"],
        # 状态字典由后端统一给，前端不另写一份，避免两边对不上
        "statuses": [
            {"value": k, "label": v, "hint": util.status_hint(k)}
            for k, v in util.STATUS_LABELS.items()
        ],
    }


def h_register(ctx):
    db = ctx.db
    username = (ctx.body.get("username") or "").strip()
    password = ctx.body.get("password") or ""
    nickname = (ctx.body.get("nickname") or "").strip() or username

    if len(username) < 2:
        raise bad_request("用户名至少 2 个字符")
    if len(username) > 32:
        raise bad_request("用户名最多 32 个字符")
    if len(password) < 6:
        raise bad_request("密码至少 6 位")

    is_first = (db.scalar("SELECT COUNT(*) FROM users") or 0) == 0
    invite_row = None

    if is_first:
        given = auth.normalize_code(ctx.body.get("initial_admin_code") or "")
        expected = auth.normalize_code(ctx.server.initial_admin_code)
        if not given or given != expected:
            raise bad_request("初始管理员邀请码不正确", "invalid_admin_code")
        is_admin = 1
    else:
        code = auth.normalize_code(ctx.body.get("invite_code") or "")
        if not code:
            raise bad_request("请填写邀请码", "invite_required")
        invite_row = db.queryone("SELECT * FROM invite_codes WHERE code = ?", (code,))
        if invite_row is None:
            raise bad_request("邀请码无效", "invalid_invite")
        if invite_row["used_by"]:
            raise bad_request("邀请码已被使用", "invite_used")
        if invite_row["expires_at"] <= util.now_iso():
            raise bad_request("邀请码已过期", "invite_expired")
        is_admin = 0

    if db.queryone("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)):
        raise conflict("用户名已存在", "username_taken")

    pw_hash, salt = auth.hash_password(password)
    cur = db.execute(
        """INSERT INTO users
           (username, password_hash, password_salt, nickname, is_admin, created_at, created_ip)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (username, pw_hash, salt, nickname, is_admin, util.now_iso(), ctx.ip),
    )
    uid = cur.lastrowid

    if invite_row is not None:
        db.execute(
            "UPDATE invite_codes SET used_by = ?, used_at = ? WHERE id = ?",
            (uid, util.now_iso(), invite_row["id"]),
        )

    return 201, {
        "ok": True,
        "is_admin": bool(is_admin),
        "user": {"id": uid, "username": username, "nickname": nickname},
    }


def h_login(ctx):
    db = ctx.db
    username = (ctx.body.get("username") or "").strip()
    password = ctx.body.get("password") or ""
    row = db.queryone(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
    )
    if row is None or not auth.verify_password(
        password, row["password_salt"], row["password_hash"]
    ):
        raise ApiError(401, "bad_credentials", "用户名或密码不正确")

    hours = int(ctx.cfg["session_hours"])
    token = auth.make_session(
        db, row["id"], ctx.ip, ctx.handler.headers.get("User-Agent", ""), hours
    )
    db.execute(
        "UPDATE users SET last_login_at = ?, last_login_ip = ? WHERE id = ?",
        (util.now_iso(), ctx.ip, row["id"]),
    )
    ctx.cookie(token, hours * 3600)
    return 200, {
        "ok": True,
        "user": {
            "id": row["id"],
            "username": row["username"],
            "nickname": row["nickname"],
            "is_admin": bool(row["is_admin"]),
        },
    }


def h_logout(ctx):
    if ctx.session is not None:
        ctx.db.execute("DELETE FROM sessions WHERE id = ?", (ctx.session["session_id"],))
    ctx.cookie("", 0)
    return 200, {"ok": True}


def h_me_get(ctx):
    user = ctx.login()
    row = ctx.db.queryone(
        """SELECT u.id, u.username, u.nickname, u.is_admin, u.avatar_hash,
                  u.created_at, u.created_ip, u.last_login_at, u.last_login_ip, a.ext AS avatar_ext
           FROM users u LEFT JOIN attachments a ON a.hash = u.avatar_hash
           WHERE u.id = ?""",
        (user["id"],),
    )
    return 200, {
        "id": row["id"],
        "username": row["username"],
        "nickname": row["nickname"],
        "is_admin": bool(row["is_admin"]),
        "avatar_url": _avatar_url(row["avatar_hash"], row["avatar_ext"]),
        "created_at": row["created_at"],
        "created_ip": row["created_ip"],
        "last_login_at": row["last_login_at"],
        "last_login_ip": row["last_login_ip"],
        "current_ip": ctx.ip,
    }


def h_me_patch(ctx):
    user = ctx.login()
    nickname = ctx.body.get("nickname")
    avatar_hash = ctx.body.get("avatar_hash", ...)

    if nickname is not None:
        nickname = str(nickname).strip()
        if not nickname or len(nickname) > 32:
            raise bad_request("昵称需为 1~32 个字符")
        ctx.db.execute("UPDATE users SET nickname = ? WHERE id = ?", (nickname, user["id"]))

    if avatar_hash is not ...:
        if avatar_hash in (None, ""):
            ctx.db.execute("UPDATE users SET avatar_hash = NULL WHERE id = ?", (user["id"],))
        else:
            att = ctx.db.queryone(
                "SELECT hash FROM attachments WHERE hash = ?", (str(avatar_hash),)
            )
            if att is None:
                raise bad_request("头像图片不存在")
            ctx.db.execute(
                "UPDATE users SET avatar_hash = ? WHERE id = ?", (att["hash"], user["id"])
            )
    return h_me_get(ctx)


def h_invite_create(ctx):
    user = ctx.login()
    hours = 24
    for _ in range(12):
        code = auth.new_invite_code()
        if not ctx.db.queryone("SELECT id FROM invite_codes WHERE code = ?", (code,)):
            break
    else:
        raise ApiError(500, "code_gen_failed", "邀请码生成失败，请重试")

    ctx.db.execute(
        """INSERT INTO invite_codes (code, created_by, created_at, expires_at)
           VALUES (?, ?, ?, ?)""",
        (code, user["id"], util.now_iso(), util.iso_after(hours=hours)),
    )
    return 201, {
        "code": code,
        "display": auth.display_code(code),
        "expires_at": util.iso_after(hours=hours),
        "valid_hours": hours,
    }


def h_invite_list(ctx):
    user = ctx.login()
    rows = ctx.db.query(
        """SELECT ic.*, ub.nickname AS used_nickname
           FROM invite_codes ic
           LEFT JOIN users ub ON ub.id = ic.used_by
           WHERE ic.created_by = ? ORDER BY ic.id DESC LIMIT 30""",
        (user["id"],),
    )
    now = util.now_iso()
    out = []
    for r in rows:
        if r["used_by"]:
            state = "used"
        elif r["expires_at"] <= now:
            state = "expired"
        else:
            state = "valid"
        out.append(
            {
                "code": r["code"],
                "display": auth.display_code(r["code"]),
                "state": state,
                "created_at": r["created_at"],
                "expires_at": r["expires_at"],
                "used_by": r["used_nickname"],
            }
        )
    return 200, {"invites": out}


# ================================================================ 内容


def h_items_list(ctx):
    ctx.login()
    db = ctx.db
    where = ["i.deleted_at IS NULL"]
    params: list = []

    kind = ctx.query.get("kind")
    if kind in ("task", "article"):
        where.append("i.kind = ?")
        params.append(kind)

    tag = (ctx.query.get("tag") or "").strip()
    if tag:
        where.append(
            """EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id
                       WHERE it.item_id = i.id AND t.name = ? COLLATE NOCASE)"""
        )
        params.append(tag)

    author = (ctx.query.get("author") or "").strip()
    if author:
        where.append("u.username = ? COLLATE NOCASE")
        params.append(author)

    q = (ctx.query.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        where.append(
            """(i.title LIKE ?
                OR EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id
                           WHERE it.item_id = i.id AND t.name LIKE ?))"""
        )
        params.extend([like, like])

    status = ctx.query.get("status")
    if status in util.STATUS_LABELS:
        where.append(
            "EXISTS (SELECT 1 FROM progress_items p WHERE p.item_id = i.id AND p.status = ?)"
        )
        params.append(status)

    clause = " AND ".join(where)
    # 默认按"最后活动时间"排序：刚被推进 / 刚有人发言的排在最前，比按创建时间更符合直觉
    sort = ("i.view_count DESC, i.id DESC" if ctx.query.get("sort") == "views"
            else "i.last_activity_at DESC, i.id DESC")

    total = db.scalar(f"SELECT COUNT(*) FROM items i JOIN users u ON u.id = i.author_id WHERE {clause}", params) or 0
    page, size, offset = _page_args(ctx)
    rows = db.query(
        ITEM_SELECT + f" WHERE {clause} ORDER BY {sort} LIMIT ? OFFSET ?",
        params + [size, offset],
    )
    ids = [r["id"] for r in rows]
    extras = _brief_rows(db, ids)
    return 200, {"total": total, "page": page, "size": size,
                 "items": [item_brief(r, extras) for r in rows]}


def h_items_create(ctx):
    user = ctx.login()
    db = ctx.db
    kind = ctx.body.get("kind")
    if kind not in ("task", "article"):
        raise bad_request("内容类型只能是「任务」或「文章」")
    title = (ctx.body.get("title") or "").strip()
    if not title:
        raise bad_request("标题不能为空")
    title = title[:200]
    body_md = ctx.body.get("body_md") or ""
    end_at = ctx.body.get("end_at") or None
    template_id = ctx.body.get("template_id")
    if template_id is not None:
        template_id = int(template_id)

    cur = db.execute(
        """INSERT INTO items
           (kind, title, body_md, author_id, created_at, created_ip, end_at,
            last_activity_at, source_template_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (kind, title, body_md, user["id"], util.now_iso(), ctx.ip, end_at,
         util.now_iso(), template_id),
    )
    item_id = cur.lastrowid

    for tid in ensure_tags(db, ctx.body.get("tags")):
        db.execute("INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?)",
                   (item_id, tid))

    titles = ctx.body.get("progress_titles") or []
    now = util.now_iso()
    for idx, pt in enumerate(titles, start=1):
        name = (str(pt).strip() or f"进度 {idx}")[:120]
        db.execute(
            """INSERT INTO progress_items (item_id, seq, title, created_at)
               VALUES (?, ?, ?, ?)""",
            (item_id, idx, name, now),
        )

    # 新建与删除一样进动态：谁在什么时候建了什么，和待办的处理方式保持一致
    payload = {"title": title, "kind": kind}
    write_event(db, item_id, None, user, ctx.ip, "item_created",
                message=util.describe("item_created", payload), payload=payload)

    return 201, {"ok": True, "id": item_id}


VIEW_WINDOW_MINUTES = 30


def _count_view(db, item_id: int, user, ip: str) -> int:
    """计一次阅读。

    同一人 30 分钟内重复打开只算一次——否则每次进度操作后前端重载详情，
    阅读量会跟着点击次数一起飙。
    """
    since = util.iso_after(minutes=-VIEW_WINDOW_MINUTES)
    dup = db.queryone(
        """SELECT id FROM view_logs
           WHERE item_id = ? AND user_id = ? AND viewed_at >= ? LIMIT 1""",
        (item_id, user["id"], since),
    )
    if dup is not None:
        return 0
    db.execute(
        "INSERT INTO view_logs (item_id, user_id, ip, viewed_at) VALUES (?, ?, ?, ?)",
        (item_id, user["id"], ip, util.now_iso()),
    )
    db.execute("UPDATE items SET view_count = view_count + 1 WHERE id = ?", (item_id,))
    return 1


def h_item_get(ctx, item_id: int):
    user = ctx.login()
    db = ctx.db
    row = _fetch_item_detail(db, item_id)
    counted = _count_view(db, item_id, user, ctx.ip)

    tags = [r["name"] for r in db.query(
        """SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id
           WHERE it.item_id = ? ORDER BY t.name""", (item_id,))]
    progs = db.query(
        "SELECT * FROM progress_items WHERE item_id = ? ORDER BY seq", (item_id,))
    progress = [progress_public(p) for p in progs]
    done = sum(1 for p in progs if p["status"] == "done")
    total = len(progs)

    return 200, {
        "id": row["id"],
        "kind": row["kind"],
        "kind_label": util.kind_label(row["kind"]),
        "title": row["title"],
        "body_md": row["body_md"],
        "author": _user_block(row, "author"),
        "tags": tags,
        "created_at": row["created_at"],
        "last_activity_at": row["last_activity_at"] or row["created_at"],
        "end_at": row["end_at"],
        "view_count": (row["view_count"] or 0) + counted,
        "progress_items": progress,
        "progress": {
            "done": done,
            "total": total,
            "percent": int(done * 100 / total) if total else 0,
        },
    }


def h_item_delete(ctx, item_id: int):
    """删除内容：**作者本人或管理员**都可以删自己创建的东西。

    早期只允许管理员，实际用起来太别扭——自己建的测试内容还要找人删。
    权限放宽后靠留痕约束：谁删的、删了什么，动态里都看得见。
    """
    user = ctx.login()
    db = ctx.db
    row = _fetch_item(db, item_id)
    if row["author_id"] != user["id"] and not user["is_admin"]:
        raise forbidden("只能删除自己创建的内容")
    db.execute(
        "UPDATE items SET deleted_at = ?, deleted_by = ? WHERE id = ?",
        (util.now_iso(), user["id"], item_id),
    )
    write_event(db, item_id, None, user, ctx.ip, "item_deleted",
                message=util.describe("item_deleted",
                                      {"title": row["title"], "kind": row["kind"]}),
                payload={"title": row["title"], "kind": row["kind"]})
    return 200, {"ok": True}


# ================================================================ 进度


def h_progress_create(ctx, item_id: int):
    ctx.login()
    db = ctx.db
    _fetch_item(db, item_id)
    titles = ctx.body.get("titles") or ctx.body.get("progress_titles") or []
    if not isinstance(titles, list) or not titles:
        raise bad_request("请填写阶段名称")
    seq = db.scalar("SELECT COALESCE(MAX(seq), 0) FROM progress_items WHERE item_id = ?", (item_id,)) or 0
    now = util.now_iso()
    created = []
    for pt in titles:
        seq += 1
        cur = db.execute(
            "INSERT INTO progress_items (item_id, seq, title, created_at) VALUES (?, ?, ?, ?)",
            (item_id, seq, (str(pt).strip() or f"进度 {seq}")[:120], now),
        )
        created.append(cur.lastrowid)
    return 201, {"ok": True, "ids": created}


def _get_progress(db, pid: int):
    row = db.queryone("SELECT * FROM progress_items WHERE id = ?", (pid,))
    if row is None:
        raise not_found("阶段不存在")
    return row


def _stages(db, item_id: int) -> list:
    return db.query(
        "SELECT * FROM progress_items WHERE item_id = ? ORDER BY seq", (item_id,)
    )


def _stage_payload(row) -> dict:
    return {"stage_id": row["id"], "seq": row["seq"], "title": row["title"]}


def h_progress_patch(ctx, pid: int):
    user = ctx.login()
    db = ctx.db
    row = _get_progress(db, pid)
    changed = False

    status = ctx.body.get("status")
    if status is not None:
        if status not in util.STATUS_LABELS:
            raise bad_request("状态取值不合法")
        if status != row["status"]:
            done_at = util.now_iso() if status == "done" else None
            # 状态与计数值同步，避免留下"已完成但计数为 0"的矛盾数据
            cursor = 1 if status == "done" else 0
            db.execute(
                "UPDATE progress_items SET status = ?, done_at = ?, cursor = ? WHERE id = ?",
                (status, done_at, cursor, pid),
            )
            payload = {**_stage_payload(row), "from": row["status"], "to": status}
            write_event(db, row["item_id"], pid, user, ctx.ip, "status_change",
                        message=util.describe("status_change", payload), payload=payload)
            changed = True

    note = ctx.body.get("note")
    if note is not None:
        note = str(note).strip()[:500]
        db.execute("UPDATE progress_items SET note = ? WHERE id = ?", (note, pid))
        payload = {**_stage_payload(row), "text": note}
        write_event(db, row["item_id"], pid, user, ctx.ip, "note",
                    message=util.describe("note", payload), payload=payload)
        changed = True

    if not changed:
        raise bad_request("没有需要修改的内容")

    fresh = _get_progress(db, pid)
    return 200, {"ok": True, "progress": progress_public(fresh)}


# --------------------------------------------- 阶段推进（任务级，不是单项计数）

def _first_open_stage(rows):
    """当前阶段 = 序号最小的未完成阶段。"""
    for i, r in enumerate(rows):
        if r["status"] != "done":
            return r, (rows[i + 1] if i + 1 < len(rows) else None)
    return None, None


def h_item_advance(ctx, item_id: int):
    """推进 +1：把当前阶段标记为已完成，并进入下一个阶段。

    语义是"往下走一个阶段"，不是在同一阶段上反复累加。
    """
    user = ctx.login()
    db = ctx.db
    _fetch_item(db, item_id)

    rows = _stages(db, item_id)
    if not rows:
        raise bad_request("这个任务还没有阶段", "no_stage")

    current, nxt = _first_open_stage(rows)
    if current is None:
        raise bad_request("所有阶段都已完成", "all_done")

    db.execute(
        "UPDATE progress_items SET status = 'done', cursor = 1, done_at = ? WHERE id = ?",
        (util.now_iso(), current["id"]),
    )
    payload = {
        **_stage_payload(current),
        "from": current["status"],
        "to": "done",
        "next_seq": nxt["seq"] if nxt else None,
        "next_title": nxt["title"] if nxt else None,
    }
    write_event(db, item_id, current["id"], user, ctx.ip, "progress_inc",
                message=util.describe("progress_inc", payload), payload=payload)
    return 200, {"ok": True, "progress": progress_public(_get_progress(db, current["id"]))}


def h_item_rollback(ctx, item_id: int):
    """回退：把最后一个已完成的阶段退回未开始。"""
    user = ctx.login()
    db = ctx.db
    _fetch_item(db, item_id)

    last = None
    for r in reversed(_stages(db, item_id)):
        if r["status"] == "done":
            last = r
            break
    if last is None:
        raise bad_request("没有可回退的阶段", "nothing_to_rollback")

    db.execute(
        "UPDATE progress_items SET status = 'pending', cursor = 0, done_at = NULL WHERE id = ?",
        (last["id"],),
    )
    payload = {**_stage_payload(last), "from": "done", "to": "pending"}
    write_event(db, item_id, last["id"], user, ctx.ip, "progress_rollback",
                message=util.describe("progress_rollback", payload), payload=payload)
    return 200, {"ok": True, "progress": progress_public(_get_progress(db, last["id"]))}


def h_item_reset(ctx, item_id: int):
    """重置：所有阶段回到未开始。"""
    user = ctx.login()
    db = ctx.db
    _fetch_item(db, item_id)

    rows = _stages(db, item_id)
    if not rows:
        raise bad_request("这个任务还没有阶段", "no_stage")

    db.execute(
        """UPDATE progress_items SET status = 'pending', cursor = 0, done_at = NULL
           WHERE item_id = ?""",
        (item_id,),
    )
    payload = {"total": len(rows)}
    write_event(db, item_id, None, user, ctx.ip, "progress_reset",
                message=util.describe("progress_reset", payload), payload=payload)
    return 200, {"ok": True, "total": len(rows)}


def h_progress_delete(ctx, pid: int):
    ctx.admin()
    db = ctx.db
    row = _get_progress(db, pid)
    db.execute("DELETE FROM progress_items WHERE id = ?", (pid,))
    return 200, {"ok": True, "item_id": row["item_id"]}


# ================================================================ 事件流


EVENT_SELECT = f"""
    SELECT e.*, {USER_COLS.format(p='actor')}
    FROM events e
    JOIN users u ON u.id = e.actor_id
    LEFT JOIN attachments au ON au.hash = u.avatar_hash
"""


def h_events_list(ctx, item_id: int):
    ctx.login()
    db = ctx.db
    _fetch_item(db, item_id)
    try:
        limit = max(1, min(200, int(ctx.query.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    before = ctx.query.get("before")

    if before:
        rows = db.query(
            EVENT_SELECT + " WHERE e.item_id = ? AND e.id < ? ORDER BY e.id DESC LIMIT ?",
            (item_id, int(before), limit),
        )
    else:
        rows = db.query(
            EVENT_SELECT + " WHERE e.item_id = ? ORDER BY e.id DESC LIMIT ?",
            (item_id, limit),
        )
    return 200, {"events": [event_public(r) for r in rows]}


def h_events_create(ctx, item_id: int):
    user = ctx.login()
    db = ctx.db
    _fetch_item(db, item_id)
    etype = ctx.body.get("type") or "comment"
    if etype not in ("comment", "note"):
        raise bad_request("只能发布消息或备注")
    message = (ctx.body.get("message") or "").strip()
    if not message:
        raise bad_request("内容不能为空")
    message = message[:2000]
    progress_id = ctx.body.get("progress_id")
    stage = None
    if progress_id is not None:
        progress_id = int(progress_id)
        stage = _stage_payload(_get_progress(db, progress_id))

    if etype == "note" and stage:
        payload = {**stage, "text": message}
        one_line = util.describe("note", payload)
    elif etype == "note":
        payload = {"text": message}
        one_line = message
    else:
        # 自由消息：正文本身就是要显示的那一行
        payload = {}
        one_line = message

    eid = write_event(db, item_id, progress_id, user, ctx.ip, etype,
                      message=one_line, payload=payload)
    return 201, {"ok": True, "id": eid}


def h_event_delete(ctx, eid: int):
    user = ctx.login()
    db = ctx.db
    row = db.queryone("SELECT * FROM events WHERE id = ?", (eid,))
    if row is None:
        raise not_found("消息不存在")
    if row["actor_id"] != user["id"] and not user["is_admin"]:
        raise forbidden("只能删除自己发布的消息")
    db.execute("DELETE FROM events WHERE id = ?", (eid,))
    return 200, {"ok": True}


def h_feed(ctx):
    """全局动态：内容类事件与待办类事件合流，按时间倒序。"""
    ctx.login()
    db = ctx.db
    try:
        limit = max(1, min(200, int(ctx.query.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20

    rows = db.query(
        EVENT_SELECT + """
        WHERE e.todo_id IS NOT NULL
           -- 待办被删后 todo_id 被置空，但事件本身要留在流里
           OR e.type LIKE 'todo_%'
           OR e.item_id IN (SELECT id FROM items WHERE deleted_at IS NULL)
           OR e.type = 'item_deleted'
        ORDER BY e.id DESC LIMIT ?""",
        (limit,),
    )

    item_ids = {r["item_id"] for r in rows if r["item_id"]}
    todo_ids = {r["todo_id"] for r in rows if r["todo_id"]}

    items = {}
    if item_ids:
        marks = ",".join("?" * len(item_ids))
        for r in db.query(
            f"SELECT id, title, kind FROM items WHERE id IN ({marks})", list(item_ids)
        ):
            items[r["id"]] = {
                "id": r["id"], "title": r["title"], "kind": r["kind"],
                "kind_label": util.kind_label(r["kind"]),
            }

    todos = {}
    if todo_ids:
        marks = ",".join("?" * len(todo_ids))
        for r in db.query(
            f"SELECT id, title, done FROM todos WHERE id IN ({marks})", list(todo_ids)
        ):
            todos[r["id"]] = {"id": r["id"], "title": r["title"], "done": bool(r["done"])}

    out = []
    for r in rows:
        ev = event_public(r)
        ev["item"] = items.get(r["item_id"])
        ev["todo"] = todos.get(r["todo_id"])
        out.append(ev)
    return 200, {"events": out}


# ================================================= 我的任务 / 待办清单 / 组员

def h_todos(ctx):
    """「我的任务」：我发起、还没走完阶段的任务。"""
    user = ctx.login()
    db = ctx.db

    mine = db.query(
        """SELECT i.id FROM items i
           WHERE i.kind = 'task' AND i.deleted_at IS NULL AND i.author_id = ?
             AND EXISTS (SELECT 1 FROM progress_items p
                         WHERE p.item_id = i.id AND p.status != 'done')
           ORDER BY i.last_activity_at DESC LIMIT 10""",
        (user["id"],),
    )
    ids = [r["id"] for r in mine]
    if not ids:
        return 200, {"my_open_tasks": []}
    marks = ",".join("?" * len(ids))
    rows = db.query(ITEM_SELECT + f" WHERE i.id IN ({marks})", ids)
    extras = _brief_rows(db, [r["id"] for r in rows])
    return 200, {"my_open_tasks": [item_brief(r, extras) for r in rows]}


def _todo_public(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "done": bool(row["done"]),
        "done_at": row["done_at"],
        "done_by": _col(row, "done_nickname"),
        "due_date": row["due_date"],
        "source": row["source"],
        "created_at": row["created_at"],
        "creator": _col(row, "creator_nickname"),
    }


def h_checklist(ctx):
    """待办清单。只展示"还没做的"；已完成的单独一组，界面默认收起。"""
    ctx.login()
    db = ctx.db
    today = ctx.query.get("date") or util.today_key()

    rows = db.query(
        """SELECT t.*, d.nickname AS done_nickname, c.nickname AS creator_nickname
           FROM todos t
           LEFT JOIN users d ON d.id = t.done_by
           LEFT JOIN users c ON c.id = t.created_by
           WHERE (t.due_date IS NULL OR t.due_date <= ?)
           ORDER BY t.done, t.id DESC""",
        (today,),
    )
    pending = [_todo_public(r) for r in rows if not r["done"]]
    done = [_todo_public(r) for r in rows if r["done"]]
    return 200, {"today": today, "pending": pending, "done": done}


def h_checklist_create(ctx):
    user = ctx.login()
    title = (ctx.body.get("title") or "").strip()
    if not title:
        raise bad_request("待办内容不能为空")
    due = (ctx.body.get("due_date") or util.today_key()).strip()[:10]
    cur = ctx.db.execute(
        """INSERT INTO todos (title, created_by, due_date, source, created_at)
           VALUES (?, ?, ?, 'manual', ?)""",
        (title[:200], user["id"], due, util.now_iso()),
    )
    tid = cur.lastrowid
    write_todo_event(ctx.db, tid, user, ctx.ip, "todo_created", title[:200])
    return 201, {"ok": True, "id": tid}


def h_checklist_patch(ctx, tid: int):
    user = ctx.login()
    row = ctx.db.queryone("SELECT * FROM todos WHERE id = ?", (tid,))
    if row is None:
        raise not_found("待办不存在")
    if "done" in ctx.body:
        done = 1 if ctx.body["done"] else 0
        if done and not row["done"]:
            ctx.db.execute(
                "UPDATE todos SET done = 1, done_at = ?, done_by = ? WHERE id = ?",
                (util.now_iso(), user["id"], tid),
            )
            write_todo_event(ctx.db, tid, user, ctx.ip, "todo_done", row["title"])
        elif not done and row["done"]:
            ctx.db.execute(
                "UPDATE todos SET done = 0, done_at = NULL, done_by = NULL WHERE id = ?",
                (tid,),
            )
            write_todo_event(ctx.db, tid, user, ctx.ip, "todo_undone", row["title"])
    if "title" in ctx.body:
        title = str(ctx.body["title"]).strip()
        if not title:
            raise bad_request("待办内容不能为空")
        ctx.db.execute("UPDATE todos SET title = ? WHERE id = ?", (title[:200], tid))
    fresh = ctx.db.queryone(
        """SELECT t.*, d.nickname AS done_nickname, c.nickname AS creator_nickname
           FROM todos t
           LEFT JOIN users d ON d.id = t.done_by
           LEFT JOIN users c ON c.id = t.created_by
           WHERE t.id = ?""",
        (tid,),
    )
    return 200, {"ok": True, "todo": _todo_public(fresh)}


def h_checklist_delete(ctx, tid: int):
    user = ctx.login()
    row = ctx.db.queryone("SELECT * FROM todos WHERE id = ?", (tid,))
    if row is None:
        raise not_found("待办不存在")
    # 先留痕再删，事件里带着标题，删掉之后动态上仍看得见发生过什么
    write_todo_event(ctx.db, tid, user, ctx.ip, "todo_deleted", row["title"])
    ctx.db.execute("DELETE FROM todos WHERE id = ?", (tid,))
    return 200, {"ok": True}


def h_members(ctx):
    """组员树：谁邀请了谁。首位注册的用户是根节点。"""
    ctx.login()
    db = ctx.db
    users = db.query(
        """SELECT u.id, u.username, u.nickname, u.is_admin, u.created_at, u.created_ip,
                  u.avatar_hash, a.ext AS avatar_ext
           FROM users u LEFT JOIN attachments a ON a.hash = u.avatar_hash
           ORDER BY u.id"""
    )
    links = db.query(
        "SELECT created_by, used_by FROM invite_codes WHERE used_by IS NOT NULL"
    )
    invited_by = {r["used_by"]: r["created_by"] for r in links}
    invited_count = {}
    for r in links:
        invited_count[r["created_by"]] = invited_count.get(r["created_by"], 0) + 1

    nodes = {}
    for u in users:
        nodes[u["id"]] = {
            "id": u["id"],
            "username": u["username"],
            "nickname": u["nickname"],
            "is_admin": bool(u["is_admin"]),
            "avatar_url": _avatar_url(u["avatar_hash"], u["avatar_ext"]),
            "created_at": u["created_at"],
            "created_ip": u["created_ip"],
            "invited_count": invited_count.get(u["id"], 0),
            "children": [],
        }

    roots = []
    for uid, node in nodes.items():
        parent = invited_by.get(uid)
        if parent and parent in nodes:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)

    flat = [nodes[u["id"]] for u in users]
    return 200, {"total": len(flat), "roots": roots, "members": flat}


# ================================================================ 模板


def h_templates_list(ctx):
    ctx.login()
    rows = ctx.db.query(
        """SELECT t.*, u.nickname AS creator FROM task_templates t
           JOIN users u ON u.id = t.created_by ORDER BY t.id DESC""",
    )
    return 200, {
        "templates": [
            {
                "id": r["id"],
                "name": r["name"],
                "payload": _json_dict(r["payload"]),
                "creator": r["creator"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }


def h_template_create(ctx):
    user = ctx.login()
    name = (ctx.body.get("name") or "").strip()
    if not name:
        raise bad_request("模板名不能为空")
    payload = ctx.body.get("payload")
    if not isinstance(payload, dict):
        raise bad_request("模板内容格式不正确")
    cur = ctx.db.execute(
        "INSERT INTO task_templates (name, payload, created_by, created_at) VALUES (?, ?, ?, ?)",
        (name[:80], json.dumps(payload, ensure_ascii=False), user["id"], util.now_iso()),
    )
    return 201, {"ok": True, "id": cur.lastrowid}


def h_template_get(ctx, tid: int):
    ctx.login()
    row = ctx.db.queryone("SELECT * FROM task_templates WHERE id = ?", (tid,))
    if row is None:
        raise not_found("模板不存在")
    return 200, {"id": row["id"], "name": row["name"], "payload": _json_dict(row["payload"])}


def h_template_delete(ctx, tid: int):
    user = ctx.login()
    db = ctx.db
    row = db.queryone("SELECT * FROM task_templates WHERE id = ?", (tid,))
    if row is None:
        raise not_found("模板不存在")
    if row["created_by"] != user["id"] and not user["is_admin"]:
        raise forbidden("只能删除自己创建的模板")

    # 用过这个模板建出来的内容，source_template_id 还指着它。
    # 外键是 RESTRICT，直接删会报 FOREIGN KEY constraint failed。
    # 先把引用断开——模板可以没有，已经建出来的内容不能受牵连。
    db.execute("UPDATE items SET source_template_id = NULL WHERE source_template_id = ?", (tid,))
    db.execute("UPDATE recurring_rules SET template_id = NULL WHERE template_id = ?", (tid,))
    db.execute("DELETE FROM task_templates WHERE id = ?", (tid,))
    return 200, {"ok": True}


# ================================================================ 定时规则


def _weekdays_str(value) -> str:
    """把 [1,2,3] 或 "1,2,3" 归一成 "1,2,3"（0=周一）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
    elif isinstance(value, (list, tuple)):
        parts = [str(p).strip() for p in value]
    else:
        parts = [str(value)]
    out = []
    for p in parts:
        try:
            n = int(p)
        except (TypeError, ValueError):
            continue
        if 0 <= n <= 6 and str(n) not in out:
            out.append(str(n))
    return ",".join(sorted(out, key=int))


def h_recurring_list(ctx):
    ctx.login()
    rows = ctx.db.query(
        """SELECT r.*, u.nickname AS creator FROM recurring_rules r
           JOIN users u ON u.id = r.created_by ORDER BY r.id DESC"""
    )
    out = []
    for r in rows:
        # 老数据只有 weekday 单值，这里统一读成 weekdays
        wd = r["weekdays"] if r["weekdays"] else (
            "" if r["weekday"] is None else str(r["weekday"]))
        out.append({
            "id": r["id"],
            "title": r["title"],
            "freq": r["freq"],
            "weekdays": [int(x) for x in wd.split(",") if x != ""],
            "time_of_day": r["time_of_day"],
            "enabled": bool(r["enabled"]),
            "creator": r["creator"],
            "last_run_at": r["last_run_at"],
            "created_at": r["created_at"],
        })
    return 200, {"rules": out}


def h_recurring_create(ctx):
    user = ctx.login()
    title = (ctx.body.get("title") or "").strip()
    freq = ctx.body.get("freq")
    tod = (ctx.body.get("time_of_day") or "").strip()
    if not title:
        raise bad_request("标题不能为空")
    if freq not in ("daily", "weekly"):
        raise bad_request("频率只能是「每天」或「每周」")
    if not re.fullmatch(r"\d{1,2}:\d{2}", tod):
        raise bad_request("时间格式应为 时:分，例如 09:00")
    hh, mm = (int(x) for x in tod.split(":"))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise bad_request("时间取值不合法")
    tod = f"{hh:02d}:{mm:02d}"

    weekdays = ""
    if freq == "weekly":
        weekdays = _weekdays_str(ctx.body.get("weekdays"))
        if not weekdays:
            raise bad_request("每周规则至少要选一个星期几")

    payload = ctx.body.get("payload") if isinstance(ctx.body.get("payload"), dict) else {}
    cur = ctx.db.execute(
        """INSERT INTO recurring_rules
           (title, freq, weekdays, time_of_day, payload, enabled, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
        (title[:200], freq, weekdays or None, tod,
         json.dumps(payload, ensure_ascii=False), user["id"], util.now_iso()),
    )
    return 201, {"ok": True, "id": cur.lastrowid}


def h_recurring_patch(ctx, rid: int):
    user = ctx.login()
    row = ctx.db.queryone("SELECT * FROM recurring_rules WHERE id = ?", (rid,))
    if row is None:
        raise not_found("规则不存在")
    if row["created_by"] != user["id"] and not user["is_admin"]:
        raise forbidden("只能修改自己创建的规则")
    if "enabled" in ctx.body:
        ctx.db.execute("UPDATE recurring_rules SET enabled = ? WHERE id = ?",
                       (1 if ctx.body["enabled"] else 0, rid))
    if "title" in ctx.body:
        ctx.db.execute("UPDATE recurring_rules SET title = ? WHERE id = ?",
                       (str(ctx.body["title"]).strip()[:200], rid))
    if "time_of_day" in ctx.body:
        tod = str(ctx.body["time_of_day"]).strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", tod):
            raise bad_request("时间格式应为 时:分，例如 09:00")
        hh, mm = (int(x) for x in tod.split(":"))
        ctx.db.execute("UPDATE recurring_rules SET time_of_day = ? WHERE id = ?",
                       (f"{hh:02d}:{mm:02d}", rid))
    if "weekdays" in ctx.body:
        wd = _weekdays_str(ctx.body["weekdays"])
        if not wd:
            raise bad_request("每周规则至少要选一个星期几")
        ctx.db.execute("UPDATE recurring_rules SET weekdays = ? WHERE id = ?", (wd, rid))
    return 200, {"ok": True}


def h_recurring_delete(ctx, rid: int):
    user = ctx.login()
    row = ctx.db.queryone("SELECT * FROM recurring_rules WHERE id = ?", (rid,))
    if row is None:
        raise not_found("规则不存在")
    if row["created_by"] != user["id"] and not user["is_admin"]:
        raise forbidden("只能删除自己创建的规则")
    ctx.db.execute("DELETE FROM recurring_rules WHERE id = ?", (rid,))
    return 200, {"ok": True}


# ================================================================ 标签 / 搜索 / 统计


def h_tags(ctx):
    """标签列表。支持按大类收窄，供"标签云跟随当前分类"的联动使用。

    计数必须数 `i.id` 而不是 `it.item_id`：后者是关联表的行，
    即使对应的内容已被删除、`i` 侧为 NULL，它依然非空，于是被算了进去。
    """
    ctx.login()
    kind = ctx.query.get("kind")
    where = ["i.deleted_at IS NULL"]
    params = []
    if kind in ("task", "article"):
        where.append("i.kind = ?")
        params.append(kind)

    rows = ctx.db.query(
        f"""SELECT t.id, t.name, COUNT(i.id) AS used
            FROM tags t
            LEFT JOIN item_tags it ON it.tag_id = t.id
            LEFT JOIN items i ON i.id = it.item_id AND {' AND '.join(where)}
            GROUP BY t.id
            HAVING used > 0
            ORDER BY used DESC, t.name""",
        params,
    )
    return 200, {
        "kind": kind if kind in ("task", "article") else "all",
        "tags": [{"id": r["id"], "name": r["name"], "used": r["used"]} for r in rows],
    }


def h_search(ctx):
    ctx.login()
    db = ctx.db
    q = (ctx.query.get("q") or "").strip()
    if not q:
        return 200, {"total": 0, "items": [], "query": q}
    scope = ctx.query.get("scope", "all")
    like = f"%{q}%"

    if scope == "title":
        clause, params = "i.title LIKE ?", [like]
    elif scope == "body":
        clause, params = "i.body_md LIKE ?", [like]
    elif scope == "events":
        clause = ("EXISTS (SELECT 1 FROM events e WHERE e.item_id = i.id AND e.message LIKE ?)")
        params = [like]
    else:
        clause = """(i.title LIKE ? OR i.body_md LIKE ?
                     OR EXISTS (SELECT 1 FROM events e WHERE e.item_id = i.id AND e.message LIKE ?))"""
        params = [like, like, like]

    where = f"i.deleted_at IS NULL AND {clause}"
    total = db.scalar(f"SELECT COUNT(*) FROM items i WHERE {where}", params) or 0
    page, size, offset = _page_args(ctx)
    rows = db.query(
        ITEM_SELECT + f" WHERE {where} ORDER BY i.created_at DESC LIMIT ? OFFSET ?",
        params + [size, offset],
    )
    extras = _brief_rows(db, [r["id"] for r in rows])
    return 200, {"total": total, "page": page, "size": size, "query": q,
                 "scope": scope, "items": [item_brief(r, extras) for r in rows]}


def h_stats(ctx):
    ctx.login()
    db = ctx.db
    return 200, {
        "users": db.scalar("SELECT COUNT(*) FROM users") or 0,
        "items": db.scalar("SELECT COUNT(*) FROM items WHERE deleted_at IS NULL") or 0,
        "tasks": db.scalar("SELECT COUNT(*) FROM items WHERE kind='task' AND deleted_at IS NULL") or 0,
        "articles": db.scalar("SELECT COUNT(*) FROM items WHERE kind='article' AND deleted_at IS NULL") or 0,
        "events": db.scalar("SELECT COUNT(*) FROM events") or 0,
        "images": db.scalar("SELECT COUNT(*) FROM attachments") or 0,
        "images_bytes": db.scalar("SELECT COALESCE(SUM(size), 0) FROM attachments") or 0,
        "tags": db.scalar("SELECT COUNT(*) FROM tags") or 0,
    }


# ================================================================ 图片上传


MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
]


def sniff_image(data: bytes):
    """读文件头判断真实类型——不信扩展名，也不信客户端 Content-Type。"""
    for magic, ext, mime in MAGIC:
        if data.startswith(magic):
            return ext, mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None, None


def parse_multipart(body: bytes, content_type: str):
    """极简 multipart/form-data 解析，只取第一个文件项。"""
    m = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not m:
        return None, None, None
    boundary = b"--" + m.group(1).encode("utf-8")
    for chunk in body.split(boundary)[1:]:
        if chunk[:2] == b"--":          # 结束标记
            break
        chunk = chunk.lstrip(b"\r\n")
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        head_text = head.decode("utf-8", "replace")
        if "filename=" not in head_text:
            continue
        fn = re.search(r'filename="([^"]*)"', head_text)
        return (fn.group(1) if fn else "upload"), head_text, data
    return None, None, None


def h_upload(ctx):
    user = ctx.login()
    data = ctx.raw_body or b""
    ctype = ctx.handler.headers.get("Content-Type", "")

    if ctype.startswith("multipart/form-data"):
        _fn, _head, data = parse_multipart(data, ctype)
        if data is None:
            raise bad_request("没有找到上传的文件")

    if not data:
        raise bad_request("上传内容为空")

    max_bytes = int(ctx.cfg["max_upload_mb"]) * 1024 * 1024
    if len(data) > max_bytes:
        raise ApiError(413, "too_large", f"图片超过 {ctx.cfg['max_upload_mb']}MB 上限")

    ext, mime = sniff_image(data)
    if ext is None:
        raise bad_request("只支持 png / jpg / gif / webp 图片", "unsupported_type")
    if ext not in [e.lower() for e in ctx.cfg["allowed_image_ext"]]:
        raise bad_request(f"不允许的图片格式：{ext}", "unsupported_type")

    digest = hashlib.sha256(data).hexdigest()[:32]
    existing = ctx.db.queryone("SELECT * FROM attachments WHERE hash = ?", (digest,))
    if existing:
        path = ctx.server.images_dir / f"{digest}.{existing['ext']}"
        if not path.exists():
            path.write_bytes(data)
        return 200, {
            "hash": digest,
            "url": f"/media/{digest}.{existing['ext']}",
            "size": existing["size"],
            "deduped": True,
        }

    (ctx.server.images_dir / f"{digest}.{ext}").write_bytes(data)
    ctx.db.execute(
        """INSERT INTO attachments (hash, ext, mime, size, uploader_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (digest, ext, mime, len(data), user["id"], util.now_iso()),
    )
    return 201, {"hash": digest, "url": f"/media/{digest}.{ext}", "size": len(data),
                 "deduped": False}
