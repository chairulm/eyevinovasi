"""Accounts, sessions and the audit log (Postgres only).

- Passwords: scrypt (stdlib hashlib), stored as  scrypt$N$r$p$salt_hex$hash_hex
- Sessions: random 256-bit token in an HttpOnly cookie; the DB keeps only its SHA-256.
- Roles: viewer (map + live video), admin (+ admin page and API).
- Brute force: an IP with 8 failed logins (or a username with 50, from anywhere) in 15 min is refused
  for a while; nginx also rate-limits the login URL.
"""
from __future__ import annotations

import hashlib
import os
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from db.store import get_store

COOKIE = "eye_session"
SESSION_DAYS = 14
MIN_PASSWORD = 10
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
ROLES = ("viewer", "admin")
MAX_FAILS = 8            # per client IP ...
MAX_FAILS_USER = 50      # per username from all IPs (high, so an attacker can't easily lock a user out)
FAIL_WINDOW_MIN = 15     # ... within this many minutes

_N, _R, _P = 2 ** 15, 8, 1


class AuthError(ValueError):
    pass


def _pool():
    store = get_store()
    if store.kind != "postgres":
        raise AuthError("database unavailable")
    return store.pool


def enabled() -> bool:
    """Accounts are on whenever a database is configured. If it is configured but down,
    auth calls raise AuthError and the server answers 503 (fail closed, never open)."""
    return bool(os.getenv("DATABASE_URL"))


# ------------------------------------------------------------------ passwords
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(h) // 2)
        return hmac.compare_digest(got.hex(), h)
    except Exception:
        return False


_DUMMY_HASH = None


def _dummy_verify(pw: str):
    """Spend the same time on unknown usernames so timing doesn't reveal which exist."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(8))
    verify_password(pw, _DUMMY_HASH)


def check_password_policy(pw: str):
    if len(pw or "") < MIN_PASSWORD:
        raise AuthError(f"password must be at least {MIN_PASSWORD} characters")


# ------------------------------------------------------------------ audit
def audit(username, action, target=None, detail=None, ip=None):
    with _pool().connection() as c:
        c.execute("INSERT INTO audit_log (username, action, target, detail, ip) VALUES (%s,%s,%s,%s,%s)",
                  (username, action, target, json.dumps(detail or {}), ip))


def list_audit(limit=100, offset=0, action=None, username=None):
    where, args = [], []
    if action:
        where.append("action = %s"); args.append(action)
    if username:
        where.append("username = %s"); args.append(username)
    sql = "SELECT id, at, username, action, target, detail, ip FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT %s OFFSET %s"
    with _pool().connection() as c:
        rows = c.execute(sql, args + [limit, offset]).fetchall()
        total = c.execute("SELECT count(*) AS n FROM audit_log" + (" WHERE " + " AND ".join(where) if where else ""),
                          args).fetchone()["n"]
    return {"total": total, "items": [_iso(r) for r in rows]}


# ------------------------------------------------------------------ users
def _iso(r):
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()}


def list_users():
    with _pool().connection() as c:
        rows = c.execute(
            "SELECT u.id, u.username, u.role, u.active, u.created_at, u.last_login, "
            "count(s.token_hash) FILTER (WHERE s.expires_at > now()) AS sessions "
            "FROM users u LEFT JOIN sessions s ON s.user_id = u.id GROUP BY u.id ORDER BY u.username").fetchall()
    return [_iso(r) for r in rows]


def get_user(user_id):
    with _pool().connection() as c:
        r = c.execute("SELECT id, username, role, active FROM users WHERE id = %s", (user_id,)).fetchone()
    return r


def count_admins(exclude_id=None):
    with _pool().connection() as c:
        return c.execute("SELECT count(*) AS n FROM users WHERE role='admin' AND active AND id IS DISTINCT FROM %s",
                         (exclude_id,)).fetchone()["n"]


def create_user(username: str, password: str, role: str):
    username = (username or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise AuthError("username: 3–32 chars, lowercase letters, digits, . _ -")
    if role not in ROLES:
        raise AuthError("role must be viewer or admin")
    check_password_policy(password)
    with _pool().connection() as c:
        if c.execute("SELECT 1 FROM users WHERE username = %s", (username,)).fetchone():
            raise AuthError("username already exists")
        r = c.execute("INSERT INTO users (username, password_hash, role) VALUES (%s,%s,%s) RETURNING id",
                      (username, hash_password(password), role)).fetchone()
    return r["id"]


def update_user(user_id: int, role=None, active=None, password=None):
    u = get_user(user_id)
    if not u:
        raise AuthError("unknown user")
    demoting = (role and role != "admin") or active is False
    if u["role"] == "admin" and u["active"] and demoting and count_admins(exclude_id=user_id) == 0:
        raise AuthError("cannot remove the last active admin")
    sets, args = [], []
    if role is not None:
        if role not in ROLES:
            raise AuthError("role must be viewer or admin")
        sets.append("role = %s"); args.append(role)
    if active is not None:
        sets.append("active = %s"); args.append(bool(active))
    if password is not None:
        check_password_policy(password)
        sets.append("password_hash = %s"); args.append(hash_password(password))
    if not sets:
        return u
    with _pool().connection() as c, c.transaction():
        c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", args + [user_id])
        if password is not None or active is False or role is not None:
            c.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))   # force re-login
    return get_user(user_id)


def delete_user(user_id: int):
    u = get_user(user_id)
    if not u:
        raise AuthError("unknown user")
    if u["role"] == "admin" and u["active"] and count_admins(exclude_id=user_id) == 0:
        raise AuthError("cannot delete the last active admin")
    with _pool().connection() as c:
        c.execute("DELETE FROM users WHERE id = %s", (user_id,))
    return u


# ------------------------------------------------------------------ login / sessions
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def too_many_failures(username: str, ip: str) -> bool:
    with _pool().connection() as c:
        r = c.execute(
            "SELECT count(*) FILTER (WHERE ip = %s) AS by_ip, count(*) FILTER (WHERE username = %s) AS by_user "
            "FROM audit_log WHERE action = 'login_failed' AND at > now() - make_interval(mins => %s)",
            (ip, username, FAIL_WINDOW_MIN)).fetchone()
    return r["by_ip"] >= MAX_FAILS or r["by_user"] >= MAX_FAILS_USER


def login(username: str, password: str, ip: str, user_agent: str):
    """Returns (token, user) or raises AuthError. Audits both outcomes."""
    username = (username or "").strip().lower()
    if too_many_failures(username, ip):
        audit(username, "login_blocked", ip=ip)
        raise AuthError("too many failed attempts — try again in 15 minutes")
    with _pool().connection() as c:
        u = c.execute("SELECT id, username, role, active, password_hash FROM users WHERE username = %s",
                      (username,)).fetchone()
    if not u:
        _dummy_verify(password)
    if not u or not u["active"] or not verify_password(password, u["password_hash"]):
        audit(username, "login_failed", ip=ip)
        raise AuthError("wrong username or password")
    token = secrets.token_urlsafe(32)
    with _pool().connection() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < now()")
        c.execute("INSERT INTO sessions (token_hash, user_id, expires_at, ip, user_agent) VALUES (%s,%s,%s,%s,%s)",
                  (_token_hash(token), u["id"], datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS),
                   ip, (user_agent or "")[:200]))
        c.execute("UPDATE users SET last_login = now() WHERE id = %s", (u["id"],))
    audit(u["username"], "login", ip=ip)
    return token, {"id": u["id"], "username": u["username"], "role": u["role"]}


def session_user(token: str | None):
    if not token:
        return None
    with _pool().connection() as c:
        r = c.execute(
            "SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = %s AND s.expires_at > now() AND u.active",
            (_token_hash(token),)).fetchone()
    return dict(r) if r else None


def logout(token: str | None):
    if token:
        with _pool().connection() as c:
            c.execute("DELETE FROM sessions WHERE token_hash = %s", (_token_hash(token),))
