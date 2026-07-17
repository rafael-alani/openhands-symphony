#!/usr/bin/env python3
"""Explicitly simulate one expired lease for the documented smoke test."""

from __future__ import annotations

import argparse
import sqlite3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("item", help="owner/repository#issue")
    parser.add_argument("--database", default="/var/lib/openhands-symphony/state.db")
    args = parser.parse_args()
    repository, number_text = args.item.rsplit("#", 1)
    issue_number = int(number_text)
    with sqlite3.connect(args.database) as connection:
        row = connection.execute(
            "SELECT id FROM jobs WHERE repository=? AND issue_number=?",
            (repository, issue_number),
        ).fetchone()
        if not row:
            raise SystemExit("smoke job not found")
        cursor = connection.execute(
            "UPDATE leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?",
            (row[0],),
        )
        if cursor.rowcount != 1:
            raise SystemExit("active lease not found")
    print(f"expired lease for run {row[0]}")


if __name__ == "__main__":
    main()
