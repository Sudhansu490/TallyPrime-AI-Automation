"""Command line: run the server, or manage user accounts on Computer A.

    python -m tally_automation                          # run the server (default)
    python -m tally_automation users list
    python -m tally_automation users create NAME --role admin [--display-name "…"]
    python -m tally_automation users set-password NAME
    python -m tally_automation users set-role NAME approver
    python -m tally_automation users deactivate NAME | activate NAME
    python -m tally_automation users unlock NAME

Passwords are typed at a hidden prompt (twice), or read from standard input
with ``--password-stdin``; never passed as command-line arguments, where they
would end up in the shell history. Deactivating, a password change and a role
change end the user's open sessions.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from tally_automation.auth.service import AuthError, create_user, end_user_sessions, normalise_username, set_password
from tally_automation.config import Settings, get_settings
from tally_automation.db.models import AppUser, UserRole
from tally_automation.db.session import create_db_engine, create_session_factory, transaction


def _password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Password again: "):
        raise SystemExit("The passwords differ; nothing was changed.")
    return first


def _user(session, username: str) -> AppUser:
    user = session.scalar(select(AppUser).where(AppUser.username == normalise_username(username)))
    if user is None:
        raise SystemExit(f"No user '{username}'.")
    return user


def users(args: argparse.Namespace, settings: Settings) -> int:
    factory = create_session_factory(create_db_engine(settings.database_url))
    try:
        with transaction(factory) as session:
            if args.action == "list":
                rows = session.scalars(select(AppUser).order_by(AppUser.username)).all()
                if not rows:
                    print("No users yet. Create the first administrator with: users create NAME --role admin")
                for u in rows:
                    state = "active" if u.is_active else "deactivated"
                    last = u.last_login_at.strftime("%Y-%m-%d %H:%M UTC") if u.last_login_at else "never"
                    print(f"{u.username:<24} {u.role.value:<9} {state:<12} last login {last}  ({u.display_name})")
            elif args.action == "create":
                user = create_user(session, settings, username=args.username, display_name=args.display_name or args.username,
                                   password=_password(args), role=UserRole(args.role))
                print(f"Created {user.username} ({user.role.value}).")
            elif args.action == "set-password":
                user = _user(session, args.username)
                set_password(session, settings, user, _password(args))
                print(f"Password changed for {user.username}; their sessions were ended.")
            elif args.action == "set-role":
                user = _user(session, args.username)
                user.role = UserRole(args.role)
                end_user_sessions(session, user.id)
                print(f"{user.username} is now {user.role.value}; their sessions were ended.")
            elif args.action in ("deactivate", "activate"):
                user = _user(session, args.username)
                user.is_active = args.action == "activate"
                if not user.is_active:
                    end_user_sessions(session, user.id)
                print(f"{user.username} {args.action}d.")
            elif args.action == "unlock":
                user = _user(session, args.username)
                user.failed_logins, user.locked_until = 0, None
                print(f"{user.username} unlocked.")
    except AuthError as exc:
        print(exc.message, file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tally_automation")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the web application (default)")
    user_parser = sub.add_parser("users", help="manage user accounts")
    actions = user_parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    roles = [r.value for r in UserRole]
    create = actions.add_parser("create")
    create.add_argument("username")
    create.add_argument("--role", choices=roles, required=True)
    create.add_argument("--display-name")
    create.add_argument("--password-stdin", action="store_true")
    password = actions.add_parser("set-password")
    password.add_argument("username")
    password.add_argument("--password-stdin", action="store_true")
    role = actions.add_parser("set-role")
    role.add_argument("username")
    role.add_argument("role", choices=roles)
    for name in ("deactivate", "activate", "unlock"):
        actions.add_parser(name).add_argument("username")

    args = parser.parse_args(argv)
    if args.command in (None, "serve"):
        from tally_automation.server import run

        run()
        return 0
    return users(args, get_settings())
