#!/usr/bin/env python3
"""Download EIA-923 (2001-2026) and EIA-860 plant/storage files into data/raw/.

Idempotent: files that already exist with a plausible size are skipped.
Run from the project root:  python3 pipeline/download.py
  --check   only report which files are present/missing (no downloading)
  --list    same, and print every URL (for downloading by hand)
"""
import sys
import urllib.request
from pathlib import Path

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)

BASE_923 = "https://www.eia.gov/electricity/data/eia923"
BASE_860 = "https://www.eia.gov/electricity/data/eia860"

FIRST_YEAR = 2001
LATEST_923 = 2026  # 2025 = early release, 2026 = monthly (through Aug 2026 as of Oct 2026)
FINAL_923 = 2024   # last year that lives under /archive/

urls = []
for y in range(FIRST_YEAR, LATEST_923 + 1):
    if y <= 2007:
        urls.append(f"{BASE_923}/archive/xls/f906920_{y}.zip")
    elif y <= FINAL_923:
        urls.append(f"{BASE_923}/archive/xls/f923_{y}.zip")
    else:
        urls.append(f"{BASE_923}/xls/f923_{y}.zip")

# EIA-860, every year. 2025 = current plants + storage inventory. 2012-2024 add
# coordinates for plants that have since retired (coordinates only exist from
# 2012). 2001-2011 have no coordinates but carry each year's generator
# retirements and street addresses, used for retirement dates / geocoding.
LATEST_860 = 2025
urls.append(f"{BASE_860}/xls/eia860{LATEST_860}.zip")
for y in range(LATEST_860 - 1, FIRST_YEAR - 1, -1):
    urls.append(f"{BASE_860}/archive/xls/eia860{y}.zip")

# EIA-860M (monthly generator inventory): one file, the newest month, downloaded
# by hand from https://www.eia.gov/electricity/data/eia860m/ (its name changes
# monthly, e.g. august_generator2026.xlsx). Each monthly file is a complete
# snapshot, so older months add nothing.
EIA860M_GLOB = "*generator*.xlsx"


def present(url: str) -> bool:
    """True if the zip (or a Safari-unzipped folder of the same name) is in data/raw."""
    name = url.rsplit("/", 1)[1]
    z = RAW / name
    return (z.exists() and z.stat().st_size > 100_000) or (RAW / name[:-4]).is_dir()


if "--check" in sys.argv or "--list" in sys.argv:
    missing = [u for u in urls if not present(u)]
    for u in urls:
        print(("  ok       " if present(u) else "  MISSING  ") + u.rsplit("/", 1)[1] + ("" if "--list" not in sys.argv else "    " + u))
    m860 = sorted(RAW.glob(EIA860M_GLOB))
    print(("  ok       " if m860 else "  MISSING  ") + "EIA-860M: " + (", ".join(f.name for f in m860) if m860 else "newest *_generator*.xlsx from https://www.eia.gov/electricity/data/eia860m/"))
    print(f"\n{len(urls) - len(missing)} of {len(urls)} zips present in {RAW}" + ("" if m860 else " (+ EIA-860M missing)"))
    sys.exit(1 if (missing or not m860) else 0)


def fetch(url: str) -> None:
    dest = RAW / url.rsplit("/", 1)[1]
    if dest.exists() and dest.stat().st_size > 100_000:
        print(f"  skip  {dest.name} (exists)")
        return
    print(f"  get   {dest.name} ...", end="", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (energy-viz data pipeline)"})
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        total = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            total += len(chunk)
    print(f" {total / 1e6:.1f} MB")


failures = []
for u in urls:
    try:
        fetch(u)
    except Exception as e:  # keep going; report at the end
        print(f" FAILED: {e}")
        failures.append(u)

if failures:
    print("\nFailed downloads (re-run to retry):")
    for u in failures:
        print("  " + u)
    sys.exit(1)
print(f"\nAll files present in {RAW}")
