"""Account admin from the shell (on the VM, as the cctv user):

  python -m server.users ensure-admin [username]   create the first admin if none exists
  python -m server.users create <username> [--role viewer|admin]
  python -m server.users reset-password <username>
  python -m server.users list

Passwords are asked interactively. When stdin isn't a terminal, a random password is
generated and printed once.
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from . import auth


def _ask_password() -> tuple[str, bool]:
    if not sys.stdin.isatty():
        return secrets.token_urlsafe(12), True
    while True:
        pw = getpass.getpass(f"  password (min {auth.MIN_PASSWORD} chars, empty = generate): ")
        if not pw:
            return secrets.token_urlsafe(12), True
        try:
            auth.check_password_policy(pw)
        except auth.AuthError as e:
            print(f"  {e}")
            continue
        if getpass.getpass("  repeat: ") == pw:
            return pw, False
        print("  passwords differ")


def _show(username, pw, generated):
    if generated:
        print(f"  login: {username}   password: {pw}")
        print("  (shown once — change it after logging in: Admin → Users)")


def _find(username):
    for u in auth.list_users():
        if u["username"] == username.lower():
            return u
    sys.exit(f"no such user: {username}")


def main():
    ap = argparse.ArgumentParser(prog="python -m server.users")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("ensure-admin"); e.add_argument("username", nargs="?", default="admin")
    c = sub.add_parser("create"); c.add_argument("username"); c.add_argument("--role", default="viewer")
    r = sub.add_parser("reset-password"); r.add_argument("username")
    sub.add_parser("list")
    a = ap.parse_args()
    if not auth.enabled():
        sys.exit("DATABASE_URL is not set — accounts need the Postgres backend")
    try:
        if a.cmd == "ensure-admin":
            if auth.count_admins() > 0:
                print("  admin account exists — skipping")
                return
            print(f"  creating the first admin account '{a.username}'")
            pw, gen = _ask_password()
            auth.create_user(a.username, pw, "admin")
            auth.audit("cli", "user_create", a.username.lower(), {"role": "admin"})
            _show(a.username.lower(), pw, gen)
        elif a.cmd == "create":
            pw, gen = _ask_password()
            auth.create_user(a.username, pw, a.role)
            auth.audit("cli", "user_create", a.username.lower(), {"role": a.role})
            print(f"  created {a.username.lower()} ({a.role})")
            _show(a.username.lower(), pw, gen)
        elif a.cmd == "reset-password":
            u = _find(a.username)
            pw, gen = _ask_password()
            auth.update_user(u["id"], password=pw, active=True if not u["active"] else None)
            auth.audit("cli", "user_update", u["username"], {"password": "reset"})
            print(f"  password reset for {u['username']} (sessions signed out)")
            _show(u["username"], pw, gen)
        elif a.cmd == "list":
            for u in auth.list_users():
                print(f"  {u['username']:<20} {u['role']:<7} {'active' if u['active'] else 'disabled':<9} "
                      f"last login {u['last_login'] or '-'}")
    except auth.AuthError as e:
        sys.exit(f"  error: {e}")


if __name__ == "__main__":
    main()
