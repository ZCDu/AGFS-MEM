"""
Manage user accounts.

    python scripts/manage_users.py add alice
    python scripts/manage_users.py add ops --admin
    python scripts/manage_users.py add reader --user-id shared-team
    python scripts/manage_users.py list
    python scripts/manage_users.py passwd alice
    python scripts/manage_users.py disable alice
    python scripts/manage_users.py enable alice
    python scripts/manage_users.py promote alice   # give an EXISTING account admin
    python scripts/manage_users.py demote alice
    python scripts/manage_users.py delete alice

Exists because of a bootstrapping problem: creating an account requires
authentication, and the first account is what you would authenticate with.
Some out-of-band path is needed, and a CLI run by whoever controls the
deployment is the smallest one — no privileged HTTP endpoint that has to be
protected forever after.

Passwords are prompted for, never passed as arguments. An argument would land
in shell history and in the process list where other users on the machine can
read it.

Accounts live in `_auth/users.json` in the same object store as everything
else, so this writes to whatever STORAGE_BACKEND points at. Check that first if
you create a user and the server cannot find it.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv()
except ImportError:
    pass

from app.config import get_settings  # noqa: E402
from app.deps import get_storage_backend  # noqa: E402
from app.users import MIN_PASSWORD_LENGTH, UserStore  # noqa: E402


def prompt_new_password() -> str:
    while True:
        a = getpass.getpass("New password: ")
        if len(a) < MIN_PASSWORD_LENGTH:
            print(f"  Too short — at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        b = getpass.getpass("Confirm: ")
        if a != b:
            print("  They do not match.")
            continue
        return a


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage memory-backend accounts.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="create an account")
    p_add.add_argument("username")
    p_add.add_argument("--user-id", default=None,
                       help="memory namespace this account reads and writes. "
                            "Defaults to the username. Point several accounts at "
                            "one user-id to share a graph.")
    p_add.add_argument("--admin", action="store_true",
                       help="may access ANY user_id")

    sub.add_parser("list", help="list accounts")
    for name, help_text in [("passwd", "change a password"),
                            ("disable", "block sign-in"),
                            ("enable", "allow sign-in"),
                            ("delete", "remove the account")]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("username")

    p_promote = sub.add_parser("promote",
        help="give an EXISTING account platform-admin (may access ANY user_id)")
    p_promote.add_argument("username")
    p_demote = sub.add_parser("demote", help="remove platform-admin from an account")
    p_demote.add_argument("username")

    args = ap.parse_args()

    settings = get_settings()
    backend = get_storage_backend()
    store = UserStore(backend)

    try:
        if args.cmd == "add":
            password = prompt_new_password()
            rec = store.create(args.username, password,
                               user_id=args.user_id, is_admin=args.admin)
            print(f"\nCreated {rec.username!r}"
                  f"{' (admin)' if rec.is_admin else ''} -> user_id {rec.user_id!r}")
            if not settings.auth_secret:
                print("\nAUTH_SECRET is not set, so login is disabled and this "
                      "account cannot be used yet. Add to .env:")
                import secrets
                print(f"    AUTH_SECRET={secrets.token_urlsafe(32)}")
            else:
                print("\nLog in with:")
                print(f'    curl.exe --% -X POST http://127.0.0.1:8000/v1/auth/login '
                      f'-H "content-type: application/json" '
                      f'-d "{{\\"username\\":\\"{rec.username}\\",\\"password\\":\\"...\\"}}"')

        elif args.cmd == "list":
            users = sorted(store.list_users(), key=lambda u: u.username)
            if not users:
                print("No accounts. Create one with: manage_users.py add <username>")
                return
            print(f"  {'USERNAME':<20}{'USER_ID':<20}{'ADMIN':<7}{'STATE':<10}CREATED")
            for u in users:
                print(f"  {u.username:<20}{u.user_id:<20}"
                      f"{'yes' if u.is_admin else '-':<7}"
                      f"{'disabled' if u.disabled else 'active':<10}{u.created_at[:19]}")

        elif args.cmd == "passwd":
            if store.get(args.username) is None:
                print(f"No such user {args.username!r}.")
                raise SystemExit(1)
            store.set_password(args.username, prompt_new_password())
            print(f"\nPassword changed for {args.username!r}.")
            print("Existing sessions stay valid until they expire — sessions are "
                  "stateless. Rotate AUTH_SECRET to end all of them now.")

        elif args.cmd in ("disable", "enable"):
            store.set_disabled(args.username, args.cmd == "disable")
            print(f"{args.username!r} is now {'disabled' if args.cmd == 'disable' else 'active'}.")
            if args.cmd == "disable":
                print("Any session token already issued keeps working until it "
                      "expires. Rotate AUTH_SECRET to cut them off immediately.")

        elif args.cmd in ("promote", "demote"):
            if store.get(args.username) is None:
                print(f"No such user {args.username!r}.")
                raise SystemExit(1)
            store.set_admin(args.username, args.cmd == "promote")
            verb = "is now a platform admin" if args.cmd == "promote" else "is no longer a platform admin"
            print(f"{args.username!r} {verb}.")
            print("Takes effect on their NEXT sign-in — a session token they "
                  "already hold keeps whatever admin flag it was issued with "
                  "until it expires (AUTH_SESSION_HOURS) or AUTH_SECRET is rotated.")

        elif args.cmd == "delete":
            if input(f"Delete account {args.username!r}? Their memory data is "
                     f"NOT removed. [y/N] ").strip().lower() != "y":
                print("Cancelled.")
                return
            store.delete(args.username)
            print(f"Deleted {args.username!r}.")

    except ValueError as e:
        print(f"Error: {e}")
        raise SystemExit(1)
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    main()
