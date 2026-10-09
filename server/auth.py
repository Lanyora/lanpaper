"""密码哈希、会话令牌、邀请码。"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from server import util

# PBKDF2 迭代次数。20 万次在普通办公机上是几十毫秒，足够且不影响体验。
PBKDF2_ROUNDS = 200_000

# 邀请码字符集：剔除 0/O、1/I/L 这类肉眼易混的字符
INVITE_ALPHABET = "ACDEFGHJKMNPQRTUVWXY23456789"
INVITE_LENGTH = 8


# ------------------------------------------------------------ 密码


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """返回 (hash_hex, salt_hex)。"""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    )
    return dk.hex(), salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    )
    return hmac.compare_digest(dk.hex(), expected_hash)


# ------------------------------------------------------------ 邀请码


def new_invite_code() -> str:
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_LENGTH))


def normalize_code(raw: str) -> str:
    """允许用户输入 XXXX-XXXX / xxxx xxxx，统一成 8 位大写。"""
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


def display_code(code: str) -> str:
    """XXXX-XXXX，方便口头转述。"""
    if len(code) == INVITE_LENGTH:
        half = INVITE_LENGTH // 2
        return f"{code[:half]}-{code[half:]}"
    return code


# ------------------------------------------------------------ 会话


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def make_session(db, user_id: int, login_ip: str, user_agent: str, hours: int) -> str:
    token = new_session_token()
    db.execute(
        """INSERT INTO sessions
           (token, user_id, created_at, expires_at, last_seen_at, login_ip, last_ip, user_agent)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token,
            user_id,
            util.now_iso(),
            util.iso_after(hours=hours),
            util.now_iso(),
            login_ip,
            login_ip,
            (user_agent or "")[:300],
        ),
    )
    return token


def resolve_session(db, token: str | None):
    """校验会话并返回对应的用户行；无效或过期返回 None。"""
    if not token:
        return None
    row = db.queryone(
        """SELECT s.id AS session_id, s.expires_at, s.user_id, s.last_ip,
                  u.id, u.username, u.nickname, u.is_admin
           FROM sessions s
           JOIN users u ON u.id = s.user_id
           WHERE s.token = ?""",
        (token,),
    )
    if row is None:
        return None
    if row["expires_at"] <= util.now_iso():
        db.execute("DELETE FROM sessions WHERE id = ?", (row["session_id"],))
        return None
    return row


def touch_session(db, session_id: int, ip: str) -> None:
    db.execute(
        "UPDATE sessions SET last_seen_at = ?, last_ip = ? WHERE id = ?",
        (util.now_iso(), ip, session_id),
    )


def purge_expired_sessions(db) -> int:
    cur = db.execute("DELETE FROM sessions WHERE expires_at <= ?", (util.now_iso(),))
    return cur.rowcount or 0
