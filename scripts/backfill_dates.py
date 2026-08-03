"""
Backfill NULL `date` values in Supabase from the legacy SQLite snapshot.

Matches live rows to `instance/fiverr_crm.db` by username:
  presales.client_username  <->  leads.client_username
  sold.client_name          <->  clients.client_name

Only touches rows where `date IS NULL` and the username maps to exactly one
distinct legacy date. Ambiguous or unmatched rows are left NULL and reported.

Usage:
    python scripts/backfill_dates.py            # dry run, writes nothing
    python scripts/backfill_dates.py --apply    # perform the update
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

SQLITE_PATH = os.path.join("instance", "fiverr_crm.db")

# (live table, live username column, sqlite table, sqlite username column)
MAPPINGS = [
    ("presales", "client_username", "leads", "client_username"),
    ("sold", "client_name", "clients", "client_name"),
]


def norm(value):
    return (value or "").strip().lower()


def legacy_dates(sq, table, column):
    """username -> set of distinct non-null dates in the SQLite snapshot."""
    out = defaultdict(set)
    for username, dt in sq.execute(f"SELECT {column}, date FROM {table}"):
        key = norm(username)
        if key and dt:
            out[key].add(str(dt)[:10])
    return out


def plan_table(cur, sq, live_table, live_col, sq_table, sq_col):
    lookup = legacy_dates(sq, sq_table, sq_col)

    cur.execute(
        f"SELECT id, {live_col} AS username FROM {live_table} "
        f"WHERE date IS NULL ORDER BY id"
    )
    rows = cur.fetchall()

    updates, ambiguous, unmatched = [], [], []
    for row in rows:
        key = norm(row["username"])
        dates = lookup.get(key)
        if not dates:
            unmatched.append((row["id"], row["username"]))
        elif len(dates) > 1:
            ambiguous.append((row["id"], row["username"], sorted(dates)))
        else:
            updates.append((row["id"], row["username"], next(iter(dates))))

    return updates, ambiguous, unmatched, len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    load_dotenv()
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        sys.exit("DATABASE_URL is not set (expected in .env)")
    if not os.path.exists(SQLITE_PATH):
        sys.exit(f"Legacy snapshot not found: {SQLITE_PATH}")

    sq = sqlite3.connect(SQLITE_PATH)
    conn = psycopg2.connect(url, cursor_factory=RealDictCursor, connect_timeout=20)
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== Date backfill [{mode}] ===\n")

    total_written = 0
    try:
        with conn:
            with conn.cursor() as cur:
                for live_table, live_col, sq_table, sq_col in MAPPINGS:
                    updates, ambiguous, unmatched, null_count = plan_table(
                        cur, sq, live_table, live_col, sq_table, sq_col
                    )

                    print(f"{live_table}: {null_count} rows with NULL date")
                    print(f"  backfill : {len(updates)}")
                    print(f"  ambiguous: {len(ambiguous)} (left NULL)")
                    print(f"  no match : {len(unmatched)} (left NULL)")

                    for rid, username, dates in ambiguous:
                        print(f"    ambiguous id={rid} {username!r} -> {dates}")
                    for rid, username in unmatched:
                        print(f"    no match  id={rid} {username!r}")

                    if args.apply and updates:
                        # Guard on `date IS NULL` so a concurrent write is never clobbered.
                        cur.executemany(
                            f"UPDATE {live_table} SET date=%s WHERE id=%s AND date IS NULL",
                            [(dt, rid) for rid, _, dt in updates],
                        )
                        total_written += len(updates)
                        print(f"  -> {len(updates)} rows updated")
                    print()

                if not args.apply:
                    print("Dry run: nothing written. Re-run with --apply to commit.")
    finally:
        conn.close()
        sq.close()

    if args.apply:
        print(f"Done. {total_written} rows updated.")


if __name__ == "__main__":
    main()
