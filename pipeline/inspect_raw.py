#!/usr/bin/env python3
"""Print the structure of raw EIA files (zip members, sheet names, header row,
columns) so the parser can be written against what is actually there.

  python3 pipeline/inspect_raw.py data/raw/eia8602005.zip            # list members
  python3 pipeline/inspect_raw.py data/raw/eia8602005.zip 'Gen'      # sheets+columns of members matching 'Gen'
  python3 pipeline/inspect_raw.py data/raw/september_generator2026.xlsx
"""
import io
import re
import sys
import zipfile

import pandas as pd


def norm(s):
    return re.sub(r"[\s_]+", " ", str(s).replace("\n", " ")).strip().lower()


def describe_workbook(name, data, max_cols=60):
    xl = pd.ExcelFile(io.BytesIO(data) if isinstance(data, bytes) else data)
    print(f"  WORKBOOK {name}: sheets = {xl.sheet_names}")
    for sheet in xl.sheet_names:
        raw = xl.parse(sheet, header=None, nrows=15)
        hdr = None
        for i in range(len(raw)):
            cells = {norm(v) for v in raw.iloc[i].values}
            if cells & {"plant id", "plant code", "plant_id", "plant_code"}:
                hdr = i
                break
        if hdr is None:
            print(f"    sheet {sheet!r}: no plant-id header in first 15 rows; row0 = {[str(v)[:25] for v in raw.iloc[0].values[:6]]}")
            continue
        cols = [norm(c) for c in raw.iloc[hdr].values if str(c) != "nan"]
        print(f"    sheet {sheet!r}: header row {hdr}, {len(cols)} cols: {cols[:max_cols]}{' ...' if len(cols) > max_cols else ''}")


path = sys.argv[1]
want = sys.argv[2] if len(sys.argv) > 2 else None
if path.lower().endswith(".zip"):
    with zipfile.ZipFile(path) as zf:
        print(f"{path}:")
        for m in zf.namelist():
            print(f"  {zf.getinfo(m).file_size:>10,}  {m}")
        if want:
            for m in zf.namelist():
                if re.search(want, m, re.I) and re.search(r"\.xlsx?$", m, re.I):
                    describe_workbook(m, zf.read(m))
else:
    describe_workbook(path, path)
