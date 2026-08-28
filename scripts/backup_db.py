"""Hot backup of the VICOBA SQLite database.

Uses the sqlite3 online backup API, so it is safe to run while the app is
serving (WAL mode included). Keeps a rotating history.

Usage:
    python scripts/backup_db.py                    # -> ./backups
    python scripts/backup_db.py --dir backups --keep 14

Schedule it daily (Windows Task Scheduler / cron) — this database holds the
group's money records; you do not want a single point of failure.
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402


def backup(dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"vicoba-{stamp}.db"

    src = sqlite3.connect(str(config.db_path()))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            with dst:
                src.backup(dst)  # online backup: consistent snapshot, WAL-safe
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def prune(dest_dir: Path, keep: int) -> None:
    files = sorted(dest_dir.glob("vicoba-*.db"))
    for old in (files[:-keep] if keep > 0 else files):
        old.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Backup the VICOBA SQLite database (WAL-safe).")
    ap.add_argument("--dir", default="backups", help="destination directory (default: ./backups)")
    ap.add_argument("--keep", type=int, default=14, help="backups to retain (default: 14)")
    args = ap.parse_args()

    dest = backup(Path(args.dir))
    prune(Path(args.dir), args.keep)
    print(f"Backup OK: {dest}")


if __name__ == "__main__":
    main()
