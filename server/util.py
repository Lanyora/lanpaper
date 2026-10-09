"""通用工具：时间、IP、事件文案、错误类型。

约定：时间统一存 UTC ISO8601 字符串，字符串排序即时间排序。
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- 时间


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return to_iso(now_dt())


def iso_after(**delta) -> str:
    """iso_after(hours=4) / iso_after(days=1)"""
    return to_iso(now_dt() + timedelta(**delta))


def iso_days_ago(days: int) -> str:
    return to_iso(now_dt() - timedelta(days=days))


def minute_key() -> str:
    """精确到分钟的时间键，供定时规则做幂等判断。"""
    return now_dt().strftime("%Y-%m-%dT%H:%M")


def today_key() -> str:
    return now_dt().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 错误


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def bad_request(message: str, code: str = "bad_request") -> ApiError:
    return ApiError(400, code, message)


def unauthorized(message: str = "请先登录") -> ApiError:
    return ApiError(401, "unauthorized", message)


def forbidden(message: str = "没有权限") -> ApiError:
    return ApiError(403, "forbidden", message)


def not_found(message: str = "内容不存在") -> ApiError:
    return ApiError(404, "not_found", message)


def conflict(message: str, code: str = "conflict") -> ApiError:
    return ApiError(409, code, message)


# ---------------------------------------------------------------- 网络


def lan_ips() -> list[str]:
    """猜出本机在局域网里的地址，供启动时打印给同事。"""
    ips: list[str] = []

    # 让系统自己选一次默认出口网卡（UDP 不会真的发包）
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("10.255.255.255", 1))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
        finally:
            sock.close()
    except OSError:
        pass

    # 兜底：枚举本机地址
    if not ips:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
        except OSError:
            pass

    return ips or ["127.0.0.1"]


def normalize_ip(raw: str, local_ip: str) -> str:
    """把回环地址换成本机的局域网地址。

    需求里 IP 的作用是"标识是哪台机器"。如果本机通过 127.0.0.1 访问，
    直接记录回环地址会让同一台机器出现两个身份（127.0.0.1 与 192.168.x.x），
    IP 就失去了区分机器的意义。所以统一换成本机局域网地址。

    真实来源不受影响：它仍然原样记录在 data/logs/app.log 里。
    """
    if not raw:
        return local_ip
    ip = raw
    if ip.startswith("::ffff:"):      # IPv4-mapped IPv6
        ip = ip[7:]
    if ip == "::1" or ip == "localhost" or ip.startswith("127."):
        return local_ip
    return ip


# ------------------------------------------------------- 事件文案


STATUS_LABELS = {
    "pending": "未开始",
    "done": "已完成",
    "blocked": "卡住",
    "delayed": "延期",
}

# 紧跟中文名显示，避免只看名字分不清"卡住"和"延期"。
# 简短版：直接放进下拉选项和悬停提示。
STATUS_HINTS = {
    "pending": "",
    "done": "",
    "blocked": "等别人或等条件",
    "delayed": "比计划慢",
}

KIND_LABELS = {"task": "任务", "article": "文章"}

# 事件类型的中文名。界面上目前只显示拼好的整句，
# 但留一份标签，避免以后任何地方把英文代码露出去。
EVENT_TYPE_LABELS = {
    "progress_inc": "推进阶段",
    "progress_reset": "重置阶段",
    "progress_rollback": "回退阶段",
    "status_change": "标记状态",
    "note": "备注",
    "comment": "留言",
    "item_created": "新建内容",
    "item_deleted": "删除内容",
    "todo_created": "新建待办",
    "todo_done": "完成待办",
    "todo_undone": "取消完成",
    "todo_deleted": "删除待办",
}


def status_label(code: str) -> str:
    return STATUS_LABELS.get(code, code)


def status_hint(code: str) -> str:
    return STATUS_HINTS.get(code, "")


def kind_label(code: str) -> str:
    return KIND_LABELS.get(code, code)


def event_type_label(code: str) -> str:
    return EVENT_TYPE_LABELS.get(code, code)


def stage_label(seq, title) -> str:
    """阶段[2]「物资准备」"""
    return f"阶段[{seq}]「{title}」"


def describe(etype: str, payload: dict) -> str:
    """在写入事件的那一刻生成一行人类可读描述。

    刻意在写入时生成而不是读取时拼装：这样即使以后阶段被改名，
    历史记录里留存的仍是当时的原话，符合"留痕"的本意。
    """
    def stage():
        return stage_label(payload.get("seq"), payload.get("title") or "")

    if etype == "progress_inc":
        if payload.get("next_title"):
            return (f"完成了 {stage()}，进入 "
                    f"{stage_label(payload.get('next_seq'), payload['next_title'])}")
        return f"完成了 {stage()}，所有阶段都已完成"

    if etype == "progress_rollback":
        return f"把 {stage()} 退回为「未开始」"

    if etype == "progress_reset":
        return f"重置了全部阶段（共 {payload.get('total', 0)} 个）"

    if etype == "status_change":
        to = STATUS_LABELS.get(payload.get("to"), payload.get("to"))
        return f"将 {stage()} 标记为了「{to}」"

    if etype == "note":
        return f"给 {stage()} 加了备注"

    if etype == "comment":
        return payload.get("text", "")

    if etype == "item_created":
        kind = KIND_LABELS.get(payload.get("kind"), "内容")
        return f"新建了{kind}「{payload.get('title', '')}」"

    if etype == "item_deleted":
        kind = KIND_LABELS.get(payload.get("kind"), "")
        # 内容被删后事件仍留在动态里，所以标题与类型都写进文案
        return (f"删除了{kind}「{payload.get('title', '')}」" if kind
                else f"删除了「{payload.get('title', '')}」")

    # ---- 待办类：待办不建任务，但同样是操作，一样要留痕 ----
    title = payload.get("title", "")
    if etype == "todo_created":
        return f"新建了待办「{title}」"
    if etype == "todo_done":
        return f"完成了待办「{title}」"
    if etype == "todo_undone":
        return f"把待办「{title}」改回未完成"
    if etype == "todo_deleted":
        return f"删除了待办「{title}」"

    return etype


def event_text(etype: str, payload: dict, message: str) -> str:
    """读取时的兜底：优先用写入时留存的原话。"""
    return message or describe(etype, payload)
