#!/usr/bin/env python
"""Set (or create) a user's password on the live database.

The old scheme was one-way, so existing passwords cannot be recovered — this
sets a known one instead. The password comes from your argument; nothing is
invented or embedded here.

    python scripts/set_password.py --email you@example.com --password 'secret'
    python scripts/set_password.py --email new@example.com --password 'secret' \
        --create --role admin --name 'Test Admin'

Add --dry-run to see what would change without writing.
"""
import argparse
import os
import sys

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
sys.path.insert(0, ROOT)

import app as crm  # noqa: E402  (needs .env loaded first)

ROLES = ("admin", "member")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--create", action="store_true",
                    help="insert the user if the email is not found")
    ap.add_argument("--role", choices=ROLES, default="member")
    ap.add_argument("--name", help="display name (only used with --create)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    email = args.email.strip().lower()
    if len(args.password) < 8:
        ap.error("password must be at least 8 characters")

    existing = crm.q1("SELECT id, name, role FROM users WHERE lower(email) = %s",
                      (email,))

    if existing:
        print("found  id=%s  name=%s  role=%s" % (
            existing["id"], existing["name"], existing["role"]))
        if args.dry_run:
            print("dry run — would reset the password for %s" % email)
            return
        crm.run("UPDATE users SET password_hash = %s WHERE id = %s",
                (crm.hash_password(args.password), existing["id"]))
        print("password reset for %s" % email)
        return

    if not args.create:
        print("no user with email %s (pass --create to add one)" % email)
        sys.exit(1)

    name = args.name or email.split("@")[0]
    if args.dry_run:
        print("dry run — would create %s (%s) as %s" % (name, email, args.role))
        return

    row = crm.insert_returning(
        "INSERT INTO users (name, email, password_hash, role) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (name, email, crm.hash_password(args.password), args.role),
    )
    print("created id=%s  %s (%s) as %s" % (row["id"], name, email, args.role))


if __name__ == "__main__":
    main()
