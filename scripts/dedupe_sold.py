"""
Remove duplicated rows from the live `sold` table.

Background: the `sold` import ran twice (~12 minutes apart), leaving every order
present exactly twice. This keeps the LOWEST id of each duplicate group (the
original import) and deletes the later copies.

Grouping key is the full business payload, not just client_name, so rows that
merely share a client but differ in any material field are never collapsed.

Usage:
    python scripts/dedupe_sold.py                 # dry run, writes nothing
    python scripts/dedupe_sold.py --backup-only   # write CSV backup, no changes
    python scripts/dedupe_sold.py --apply         # backup, then delete
"""
import argparse
import csv
import os
import sys
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Fields that define a duplicate. `id`, `created_at`, `updated_at` deliberately
# excluded -- those differ between the two import runs by design.
KEY_FIELDS = [
    "client_name", "project_name", "account", "service_type", "order_id",
    "status", "assign_leader", "developer", "deli_last_date",
    "order_amount", "bonus_amount", "sheet_link", "comment", "presale_id",
]


def fetch_all(cur):
    cur.execute("SELECT * FROM sold ORDER BY id")
    return cur.fetchall()


def write_backup(rows):
    os.makedirs("backups", exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join("backups", f"sold_before_dedupe_{stamp}.csv")
    if not rows:
        print("  nothing to back up")
        return None
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"  backup written: {path} ({len(rows)} rows)")
    return path


def plan(rows):
    """Group by business payload. Returns (keep_ids, delete_ids, singletons)."""
    groups = {}
    for row in rows:
        key = tuple(str(row.get(f)) for f in KEY_FIELDS)
        groups.setdefault(key, []).append(row)

    keep, delete, singles = [], [], 0
    for key, members in groups.items():
        members.sort(key=lambda r: r["id"])
        if len(members) == 1:
            singles += 1
            continue
        keep.append(members[0])
        delete.extend(members[1:])
    return keep, delete, singles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform the delete")
    ap.add_argument("--backup-only", action="store_true", help="write CSV backup and exit")
    args = ap.parse_args()

    load_dotenv()
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        sys.exit("DATABASE_URL is not set (expected in .env)")

    conn = psycopg2.connect(url, cursor_factory=RealDictCursor, connect_timeout=20)
    mode = "APPLY" if args.apply else ("BACKUP ONLY" if args.backup_only else "DRY RUN")
    print(f"=== sold dedupe [{mode}] ===\n")

    try:
        with conn:
            with conn.cursor() as cur:
                rows = fetch_all(cur)
                print(f"live sold rows: {len(rows)}")

                if args.backup_only or args.apply:
                    write_backup(rows)
                    if args.backup_only:
                        print("\nBackup complete. Nothing deleted.")
                        return

                keep, delete, singles = plan(rows)
                print(f"  duplicate groups : {len(keep)}")
                print(f"  unique rows      : {singles}")
                print(f"  rows to delete   : {len(delete)}\n")

                if delete:
                    print("  KEEP (original)              DELETE (later copy)")
                    by_key = {}
                    for row in keep:
                        by_key[tuple(str(row.get(f)) for f in KEY_FIELDS)] = row
                    for row in delete:
                        original = by_key[tuple(str(row.get(f)) for f in KEY_FIELDS)]
                        print(
                            f"  id={original['id']:<4} {str(original['client_name'])[:18]:<18}"
                            f"  ->  id={row['id']:<4} {str(row['client_name'])[:18]:<18}"
                            f" ${row['order_amount']}"
                        )
                    print()

                    # Referential safety: activities.sold_id points at these rows.
                    ids = [r["id"] for r in delete]
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM activities WHERE sold_id = ANY(%s)", (ids,)
                    )
                    refs = cur.fetchone()["n"]
                    print(f"  activities referencing doomed rows: {refs}")
                    if refs:
                        print("  -> those activity rows would be orphaned; resolve before applying")

                    total_before = sum(float(r["order_amount"] or 0) + float(r["bonus_amount"] or 0) for r in rows)
                    total_after = total_before - sum(
                        float(r["order_amount"] or 0) + float(r["bonus_amount"] or 0) for r in delete
                    )
                    print(f"  revenue before: ${total_before:,.2f}")
                    print(f"  revenue after : ${total_after:,.2f}")

                    if args.apply:
                        if refs:
                            sys.exit("\nAborted: activities reference rows marked for deletion.")
                        cur.execute("DELETE FROM sold WHERE id = ANY(%s)", (ids,))
                        print(f"\n  -> {cur.rowcount} rows deleted")
                    else:
                        print("\nDry run: nothing written. Re-run with --apply to commit.")
                else:
                    print("No duplicates found.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
