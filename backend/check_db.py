"""DB diagnostic — answers 'what's eating my Mongo storage?'

Loads the same config the live server loads (so MONGO_URL etc. come
from .env), pings the server, then prints:
  - the resolved MONGO_URL (redacted)
  - the total dataSize across all collections in the active DB
  - a per-collection breakdown sorted by size (largest first), showing
    document count, total dataSize, total indexSize, and average doc
    size
  - GridFS bucket sizes (payslip_pdfs etc.) if any

Run with:
  backend\\venv\\Scripts\\python.exe backend\\check_db.py
"""

import asyncio
import sys
from pathlib import Path

# Make sure we resolve imports the same way main.py does.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402  — triggers the .env load
from database import client, db, MONGO_URL, MONGO_DB_NAME  # noqa: E402


def _redact(url: str) -> str:
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        creds, host = rest.split("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return url


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


async def main() -> None:
    print("=" * 70)
    print(f"MONGO_URL     : {_redact(MONGO_URL)}")
    print(f"MONGO_DB_NAME : {MONGO_DB_NAME}")
    print("=" * 70)

    # Ping
    try:
        await client.admin.command("ping")
        print("Ping          : OK")
    except Exception as e:  # noqa: BLE001
        print(f"Ping          : FAILED — {e}")
        return

    # DB stats — overall size
    try:
        dbstats = await db.command("dbStats")
        print(
            f"DB total      : "
            f"{_fmt_bytes(dbstats.get('dataSize', 0))} data + "
            f"{_fmt_bytes(dbstats.get('indexSize', 0))} indexes = "
            f"{_fmt_bytes(dbstats.get('storageSize', 0))} on disk"
        )
        print(
            f"Collections   : {dbstats.get('collections', 0)}, "
            f"Objects: {dbstats.get('objects', 0):,}"
        )
    except Exception as e:  # noqa: BLE001
        print(f"dbStats failed: {e}")

    print("-" * 70)
    print("PER COLLECTION (sorted by data size, largest first)")
    print("-" * 70)
    print(
        f"{'collection':<30} {'count':>10} {'data':>12} {'indexes':>12} "
        f"{'avg doc':>10}"
    )
    print("-" * 70)

    # Per-collection breakdown
    collections = await db.list_collection_names()
    rows = []
    for name in collections:
        try:
            stats = await db.command("collStats", name)
            rows.append({
                "name": name,
                "count": stats.get("count", 0),
                "size": stats.get("size", 0),
                "indexSize": stats.get("totalIndexSize", 0),
                "avgObjSize": stats.get("avgObjSize", 0),
            })
        except Exception as e:  # noqa: BLE001
            print(f"  (collStats failed for {name}: {e})")

    rows.sort(key=lambda r: r["size"], reverse=True)
    grand_total_data = 0
    grand_total_idx = 0
    for r in rows:
        grand_total_data += r["size"]
        grand_total_idx += r["indexSize"]
        if r["count"] == 0 and r["size"] == 0:
            continue
        print(
            f"{r['name']:<30} "
            f"{r['count']:>10,} "
            f"{_fmt_bytes(r['size']):>12} "
            f"{_fmt_bytes(r['indexSize']):>12} "
            f"{_fmt_bytes(r['avgObjSize']):>10}"
        )
    print("-" * 70)
    print(
        f"{'TOTAL':<30} "
        f"{sum(r['count'] for r in rows):>10,} "
        f"{_fmt_bytes(grand_total_data):>12} "
        f"{_fmt_bytes(grand_total_idx):>12}"
    )
    print("=" * 70)

    # GridFS specifically — payslip_pdfs lives there and can be huge
    gridfs_buckets = [
        n.replace(".files", "")
        for n in collections
        if n.endswith(".files")
    ]
    if gridfs_buckets:
        print()
        print("GridFS BUCKETS (binary blobs — these inflate Mongo fast)")
        print("-" * 70)
        for bucket in gridfs_buckets:
            try:
                chunks_stats = await db.command(
                    "collStats", f"{bucket}.chunks",
                )
                files_stats = await db.command(
                    "collStats", f"{bucket}.files",
                )
                file_count = files_stats.get("count", 0)
                total = (
                    chunks_stats.get("size", 0)
                    + files_stats.get("size", 0)
                )
                print(
                    f"  {bucket}: {file_count:,} files, "
                    f"{_fmt_bytes(total)} total"
                )
            except Exception as e:  # noqa: BLE001
                print(f"  {bucket}: stats failed ({e})")
        print("-" * 70)


if __name__ == "__main__":
    asyncio.run(main())
