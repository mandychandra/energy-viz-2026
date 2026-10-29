#!/usr/bin/env python3
"""EIA-923 + EIA-860 -> GeoJSON for the energy map.

Reads every EIA-923 zip found in data/raw/ (any subset of years works), joins
plant coordinates from every EIA-860 zip found, and writes to data/out/:

  plant_generation.geojson   one point per (plant, fuel category); monthly MWh
                             as netgen_<year>_<month0>   (zeros omitted)
  quarterly_generation.geojson  same points; quarterly MWh as netgen_<year>_<q0>
  plant_labels.geojson       name, fuel, lifetime GWh, storage MW/MWh/tech/year
  fuel_totals.json           months list, per-fuel TWh by year, normalisation
  annual_twh.csv             sanity table: TWh per fuel per year

Fuel categories (0-9 are Chris Loer's; 11 is new):
  0 Gas  1 Coal  2 Nuclear  3 Hydro  4 Wind  5 Solar  6 Bio (renewable carbon)
  7 Other  8 Geothermal  9 Petroleum  11 Storage (battery / pumped / flywheel / CAES)

Storage is keyed by PRIME MOVER (BA, PS, FW, CE, ES), not fuel code, and its
monthly value is GROSS DISCHARGE from the "Page 1 Energy Storage" sheet
(2014+). Net generation for storage is negative (round-trip losses) and would
make it invisible on the map -- that is why it is missing from the 2019 map.

Per-year parsing is cached in data/cache/ so re-runs take seconds.
Run from the project root:  python3 pipeline/build.py
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW, CACHE, OUT = ROOT / "data" / "raw", ROOT / "data" / "cache", ROOT / "data" / "out"
CACHE.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MONTH_FULL = ["january", "february", "march", "april", "may", "june", "july", "august",
              "september", "october", "november", "december"]

FUEL_NAMES = {0: "Gas", 1: "Coal", 2: "Nuclear", 3: "Hydro", 4: "Wind", 5: "Solar",
              6: "Bio", 7: "Other", 8: "Geothermal", 9: "Petroleum", 11: "Storage"}

# EIA "Reported Fuel Type Code" -> category. Chris Loer's map plus newer codes.
FUEL_CODE = {
    "NG": 0, "OG": 0, "BFG": 0, "H2": 0,
    "SUB": 1, "LIG": 1, "RC": 1, "WC": 1, "SGC": 1, "ANT": 1, "BIT": 1,
    "NUC": 2,
    "WAT": 3,
    "WND": 4,
    "SUN": 5,
    "WDS": 6, "BLQ": 6, "OBG": 6, "AB": 6, "MSW": 6, "OBS": 6, "OBL": 6, "SLW": 6, "WDL": 6, "LFG": 6,
    "MSB": 7, "MSN": 7, "WH": 7, "PUR": 7, "TDF": 7, "OTH": 7, "SC": 7,
    "GEO": 8,
    "DFO": 9, "JF": 9, "KER": 9, "PC": 9, "PG": 9, "RFO": 9, "SGP": 9, "WO": 9,
    "MWH": 11,
    # codes used only in the 2001-2007 EIA-906/920 files
    "COL": 1, "OOG": 0, "HPS": 11, "OW": 6,
}
STORAGE_PM = {"BA": "battery", "PS": "pumped", "FW": "flywheel", "CE": "caes", "ES": "other"}
STORAGE_CAT = 11
PLACEHOLDER_PLANT = 99999  # EIA state-level non-response estimates, not a plant


# ----------------------------------------------------------------------------
# Excel helpers
# ----------------------------------------------------------------------------
def norm(s) -> str:
    """Normalise a header cell: lower-case, collapse whitespace/underscores."""
    return re.sub(r"[\s_]+", " ", str(s).replace("\n", " ")).strip().lower()


def find_header_row(raw: pd.DataFrame, keys=("plant id", "plant code")) -> int:
    for i in range(min(len(raw), 25)):
        cells = {norm(v) for v in raw.iloc[i].values}
        if cells & set(keys):
            return i
    raise ValueError(f"no header row containing {keys}")


def read_sheet(xl: pd.ExcelFile, sheet, keys=("plant id", "plant code")) -> pd.DataFrame:
    raw = xl.parse(sheet, header=None, nrows=25)
    hdr = find_header_row(raw, keys)
    df = xl.parse(sheet, header=hdr)
    df.columns = [norm(c) for c in df.columns]
    return df


def month_columns(df: pd.DataFrame, prefix: str) -> list[str]:
    """Return the 12 monthly columns for e.g. prefix 'netgen' in calendar order."""
    cols = []
    for ab, full in zip(MONTHS, MONTH_FULL):
        hit = [c for c in df.columns if re.fullmatch(rf"{prefix}\s?(?:{ab}|{full})", c)]
        if not hit:
            raise ValueError(f"missing {prefix} column for {full}")
        cols.append(hit[0])
    return cols


class Archive:
    """A downloaded EIA zip, or the folder Safari unzipped it into."""

    def __init__(self, path: Path):
        self.path, self.name = path, path.name
        self.zf = None if path.is_dir() else zipfile.ZipFile(path)

    def names(self):
        if self.zf:
            return self.zf.namelist()
        return [str(p.relative_to(self.path)) for p in self.path.rglob("*") if p.is_file()]

    def size(self, m):
        return self.zf.getinfo(m).file_size if self.zf else (self.path / m).stat().st_size

    def read(self, m):
        return self.zf.read(m) if self.zf else (self.path / m).read_bytes()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        if self.zf:
            self.zf.close()


def pick_member(zf: Archive, include: str, exclude: str = r"$^") -> str:
    cands = [m for m in zf.names() if re.search(r"\.xlsx?$", m, re.I) and not Path(m).name.startswith("~$")
             and re.search(include, m, re.I) and not re.search(exclude, m, re.I)]
    if not cands:
        raise ValueError(f"no member matching {include!r} in {zf.name}")
    return max(cands, key=zf.size)


def to_num(s: pd.Series) -> pd.Series:
    if s.dtype == object:
        s = s.astype(str).str.replace(",", "", regex=False)
    return pd.to_numeric(s, errors="coerce").fillna(0)


# ----------------------------------------------------------------------------
# EIA-923: one tidy frame per year, cached
#   columns: plant_code, plant_name, state, pm, fuel, cat, stype, m0..m11
# ----------------------------------------------------------------------------
def parse_923(zip_path: Path, year: int) -> pd.DataFrame:
    cache = CACHE / f"gen_{year}.csv"
    if cache.exists():
        df = pd.read_csv(cache, dtype={"fuel": str, "pm": str, "stype": str, "state": str, "plant_name": str})
        for c in ("stype", "fuel", "pm", "plant_name", "state"):
            df[c] = df[c].fillna("")
        return df

    print(f"  parsing EIA-923 {year} ({zip_path.name}) ...", flush=True)
    with Archive(zip_path) as zf:
        member = pick_member(zf, include=r"schedules?[ _]?2_3_4_5|f906920|eia923",
                             exclude=r"envir|schedule[ _]?8|6_7|source|disposition")
        xl = pd.ExcelFile(io.BytesIO(zf.read(member)))

    gen_sheet = next((s for s in xl.sheet_names if re.search(r"page 1 generation", s, re.I)), xl.sheet_names[0])
    g = read_sheet(xl, gen_sheet)
    ng = month_columns(g, "netgen")
    # In-year monthly files carry EIA's own estimates for plants that only report
    # annually, as "State-Fuel Level Increment" rows under plant 99999. Keep them
    # (cached separately) so a partial year can be completed, see impute_year().
    est = g[g["plant id"] == PLACEHOLDER_PLANT]
    if len(est) > 50:
        e = pd.DataFrame({
            "state": est[next(c for c in est.columns if c in ("plant state", "state"))].astype(str),
            "pm": est["reported prime mover"].astype(str).str.strip().str.upper(),
            "fuel": est["reported fuel type code"].astype(str).str.strip().str.upper(),
        })
        for i, c in enumerate(ng):
            e[f"m{i}"] = to_num(est[c]).values
        e["cat"] = e["fuel"].map(FUEL_CODE).fillna(7).astype(int)
        e.loc[e["pm"].isin(STORAGE_PM), "cat"] = STORAGE_CAT
        e.to_csv(CACHE / f"est_{year}.csv", index=False)
        print(f"    {len(e)} state-level EIA estimate rows kept ({e[[f'm{i}' for i in range(12)]].sum().sum() / 1e6:,.0f} TWh)")
    g = g[g["plant id"] != PLACEHOLDER_PLANT].copy()
    df = pd.DataFrame({
        "plant_code": g["plant id"].astype(int),
        "plant_name": g["plant name"].astype(str).str.strip(),
        "state": g[next(c for c in g.columns if c in ("plant state", "state"))].astype(str),
        "pm": g["reported prime mover"].astype(str).str.strip().str.upper(),
        "fuel": g["reported fuel type code"].astype(str).str.strip().str.upper(),
    })
    for i, c in enumerate(ng):
        df[f"m{i}"] = to_num(g[c]).values

    # Storage: Page 1 reports storage units with NET generation (discharge minus
    # charging = negative). From 2014 the "Energy Storage" sheet repeats the same
    # rows with GROSS generation (= discharge). Use those rows instead of Page 1's
    # for storage prime movers, so each storage unit appears exactly once.
    sto_sheet = next((s for s in xl.sheet_names if re.search(r"energy storage", s, re.I)), None)
    # 2001-2003 code pumped storage as fuel HPS under prime mover HY; treat as PS
    df.loc[df["fuel"] == "HPS", "pm"] = "PS"
    is_storage = df["pm"].isin(STORAGE_PM)
    n_p1 = int(is_storage.sum())
    if sto_sheet:
        s = read_sheet(xl, sto_sheet)
        s = s[s["reported prime mover"].astype(str).str.strip().str.upper().isin(STORAGE_PM)]
        s = s[s["plant id"] != PLACEHOLDER_PLANT]
        gg = month_columns(s, "grossgen")
        sto = pd.DataFrame({
            "plant_code": s["plant id"].astype(int),
            "plant_name": s["plant name"].astype(str).str.strip(),
            "state": s[next(c for c in s.columns if c in ("plant state", "state"))].astype(str),
            "pm": s["reported prime mover"].astype(str).str.strip().str.upper(),
            "fuel": s["reported fuel type code"].astype(str).str.strip().str.upper(),
        })
        for i, c in enumerate(gg):
            sto[f"m{i}"] = to_num(s[c]).values
        df = pd.concat([df[~is_storage], sto], ignore_index=True)
        print(f"    storage: {n_p1} Page-1 rows (net) replaced by {len(sto)} Energy-Storage rows (gross discharge)")
    else:
        # No storage sheet before 2014: net generation of storage is negative and
        # meaningless as output -> contributes nothing (storage series starts 2014)
        for i in range(12):
            df.loc[is_storage, f"m{i}"] = 0.0
    is_storage = df["pm"].isin(STORAGE_PM)

    df["cat"] = df["fuel"].map(FUEL_CODE)
    unknown = sorted(df.loc[df["cat"].isna(), "fuel"].unique())
    if unknown:
        print(f"    unmapped fuel codes -> Other: {unknown}")
    df["cat"] = df["cat"].fillna(7).astype(int)
    df.loc[is_storage, "cat"] = STORAGE_CAT
    df["stype"] = df["pm"].map(STORAGE_PM).fillna("")
    df["year"] = year
    df.to_csv(cache, index=False)
    return df


# ----------------------------------------------------------------------------
# EIA-860: plant coordinates (newest file wins, older files fill gaps) and the
# storage inventory (3_4_Energy_Storage) for labels
# ----------------------------------------------------------------------------
def load_860(zips: list[Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
    coords, storage = {}, None
    for zp in sorted(zips, reverse=True):  # eia8602025 before eia8602017 ...
        with Archive(zp) as zf:
            try:
                pm = pick_member(zf, include=r"plant", exclude=r"layout|form|instruct")
                p = read_sheet(pd.ExcelFile(io.BytesIO(zf.read(pm))), 0, keys=("plant code",))
            except (ValueError, KeyError) as e:
                print(f"  {zp.name}: no readable plant file ({str(e)[:60]}), skipped")
                continue
            if "latitude" not in p.columns or "longitude" not in p.columns:
                print(f"  {zp.name}: plant file has no coordinates (EIA added them in 2012), skipped")
                continue
            lat, lon = to_num(p["latitude"]), to_num(p["longitude"])
            added = 0
            for code, la, lo, nm, st in zip(p["plant code"], lat, lon, p["plant name"], p["state"]):
                try:
                    code = int(code)
                except (TypeError, ValueError):
                    continue
                if code in coords or not (-90 < la < 90 and -180 < lo < 180 and la != 0):
                    continue
                coords[code] = (round(float(lo), 5), round(float(la), 5), str(nm).strip(), str(st))
                added += 1
            print(f"  {zp.name}: {added} plant coordinates added (total {len(coords)})")

            if storage is None:
                try:
                    sm = pick_member(zf, include=r"energy[_ ]?storage")
                except ValueError:
                    continue
                xl = pd.ExcelFile(io.BytesIO(zf.read(sm)))
                frames = []
                for sheet in xl.sheet_names:
                    if re.search(r"operable|retired", sheet, re.I):
                        s = read_sheet(xl, sheet, keys=("plant code",))
                        # keep operating/standby units and true retirements; drop canceled/postponed
                        s = s[s["status"].astype(str).str.upper().isin(["OP", "OS", "OA", "SB", "RE"])]
                        frames.append(s)
                s = pd.concat(frames, ignore_index=True)
                s = pd.DataFrame({
                    "plant_code": pd.to_numeric(s["plant code"], errors="coerce"),
                    "pm": s["prime mover"].astype(str).str.upper(),
                    "mw": to_num(s["nameplate capacity (mw)"]),
                    "mwh": to_num(s["nameplate energy capacity (mwh)"]),
                    "tech": s["storage technology 1"].astype(str).str.upper().str.strip(),
                    "op_year": pd.to_numeric(s["operating year"], errors="coerce"),
                }).dropna(subset=["plant_code"])
                storage = (s.groupby(["plant_code", "pm"], as_index=False)
                            .agg(mw=("mw", "sum"), mwh=("mwh", "sum"),
                                 tech=("tech", lambda t: t.mode().iat[0] if len(t.mode()) else ""),
                                 op_year=("op_year", "min")))
                storage["plant_code"] = storage["plant_code"].astype(int)
                print(f"  {zp.name}: storage inventory {len(storage)} (plant, prime mover) units, "
                      f"{storage['mw'].sum():,.0f} MW")
    # Pumped storage capacity lives in the generator file, not 3_4_Energy_Storage
    with Archive(sorted(zips, reverse=True)[0]) as zf:
        try:
            gm = pick_member(zf, include=r"generator", exclude=r"layout|form|instruct")
            xl = pd.ExcelFile(io.BytesIO(zf.read(gm)))
            g = read_sheet(xl, [s for s in xl.sheet_names if re.search("operable", s, re.I)][0], keys=("plant code",))
            ps = g[g["prime mover"].astype(str).str.upper() == "PS"]
            ps = pd.DataFrame({"plant_code": pd.to_numeric(ps["plant code"], errors="coerce"),
                               "pm": "PS", "mw": to_num(ps["nameplate capacity (mw)"]), "mwh": 0.0,
                               "tech": "PUMPED", "op_year": pd.to_numeric(ps["operating year"], errors="coerce")})
            ps = ps.dropna(subset=["plant_code"]).groupby(["plant_code", "pm"], as_index=False).agg(
                mw=("mw", "sum"), mwh=("mwh", "sum"), tech=("tech", "first"), op_year=("op_year", "min"))
            ps["plant_code"] = ps["plant_code"].astype(int)
            storage = pd.concat([storage, ps], ignore_index=True)
            print(f"  pumped storage from generator file: {len(ps)} plants, {ps['mw'].sum():,.0f} MW")
        except (ValueError, IndexError) as e:
            print(f"  (no generator file for pumped storage capacity: {e})")
    cdf = pd.DataFrame.from_dict(coords, orient="index", columns=["lon", "lat", "name860", "state860"])
    cdf.index.name = "plant_code"
    return cdf, storage


# ----------------------------------------------------------------------------
# Completing an in-year (sample-only) file. EIA's monthly file has the ~3,500
# monthly reporters at plant level plus state x fuel estimates for everyone
# else. Allocate each estimate across that state's non-reporting plants in
# proportion to their previous-year output for the same month (EIA uses the
# same proportional method when it finalises a year). Storage estimates are
# net-based and unusable, so non-reporting storage units carry forward their
# previous-year gross discharge. Every imputed row is flagged est=True.
# ----------------------------------------------------------------------------
def impute_year(gen: pd.DataFrame, year: int, est: pd.DataFrame) -> pd.DataFrame:
    mcols = [f"m{i}" for i in range(12)]
    real = gen[gen["year"] == year]
    prev = gen[gen["year"] == year - 1]
    data_months = [m for m in mcols if real[m].abs().sum() > 0]
    real_keys = set(zip(real["plant_code"], real["cat"]))
    cand = prev.groupby(["plant_code", "cat"], as_index=False).agg(
        state=("state", "first"), plant_name=("plant_name", "first"), pm=("pm", "first"),
        fuel=("fuel", "first"), stype=("stype", "first"), **{m: (m, "sum") for m in mcols})
    cand = cand[[(p, c) not in real_keys for p, c in zip(cand["plant_code"], cand["cat"])]].copy()
    cand[mcols] = cand[mcols].clip(lower=0)
    cand["annual"] = cand[mcols].sum(axis=1)
    cand = cand[cand["annual"] > 0]

    rows, dropped, placed = [], 0.0, 0.0
    E = est[est["cat"] != STORAGE_CAT].groupby(["state", "cat"])[mcols].sum()
    for (st, cat), e in E.iterrows():
        grp = cand[(cand["state"] == st) & (cand["cat"] == cat)]
        if grp.empty:
            dropped += float(e.clip(lower=0).sum())
            continue
        out = grp[["plant_code", "cat", "state", "plant_name", "pm", "fuel", "stype"]].copy()
        for m in mcols:
            if m not in data_months or e[m] <= 0:
                out[m] = 0.0
                continue
            w = grp[m].values if grp[m].sum() > 0 else grp["annual"].values
            out[m] = e[m] * w / w.sum()
            placed += float(e[m])
        rows.append(out)
    sto = cand[cand["cat"] == STORAGE_CAT][["plant_code", "cat", "state", "plant_name", "pm", "fuel", "stype"] + mcols].copy()
    for m in mcols:
        if m not in data_months:
            sto[m] = 0.0
    carried = float(sto[mcols].sum().sum())
    imp = pd.concat(rows + [sto], ignore_index=True)
    imp["year"] = year
    imp["est"] = True
    print(f"  {year}: completed from EIA state-level estimates: {placed / 1e6:,.1f} TWh allocated to "
          f"{imp[imp['cat'] != STORAGE_CAT]['plant_code'].nunique():,} non-reporting plants, "
          f"{dropped / 1e6:,.1f} TWh unplaceable (no matching plants); storage carried forward {carried / 1e6:,.2f} TWh "
          f"for {sto['plant_code'].nunique()} units")
    return imp


# ----------------------------------------------------------------------------
# EIA-860M (monthly generator inventory, newest file): the unit timeline.
# Per (plant, fuel category): first in-service month, retirement month (only when
# every unit of that category is retired), status, operating MW/MWh, coordinates,
# plus planned units. Used for dates, status and coordinate gap-filling - never
# for generation quantities.
# ----------------------------------------------------------------------------
MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july",
               "august", "september", "october", "november", "december"]


def newest_860m() -> Path | None:
    files = list(RAW.glob("*generator*.xlsx"))
    def key(p):
        m = re.match(r"([a-z]+)_generator(\d{4})", p.name.lower())
        return (int(m.group(2)), MONTH_NAMES.index(m.group(1))) if m and m.group(1) in MONTH_NAMES else (0, 0)
    return max(files, key=key) if files else None


def yyyymm(year: pd.Series, month: pd.Series) -> pd.Series:
    y, m = pd.to_numeric(year, errors="coerce"), pd.to_numeric(month, errors="coerce")
    out = y * 100 + m.fillna(1).clip(1, 12)
    return out.where(y.notna() & (y > 1800))


def load_860m(path: Path):
    xl = pd.ExcelFile(path)
    tabs = {}
    for name in ("Operating", "Retired", "Planned"):
        sheet = next((sh for sh in xl.sheet_names if sh.lower() == name.lower()), None)
        if sheet is None:
            continue
        t = read_sheet(xl, sheet, keys=("plant id",))
        t = t[pd.to_numeric(t["plant id"], errors="coerce").notna()].copy()
        t["plant_code"] = t["plant id"].astype(int)
        pmc = t["prime mover code"].astype(str).str.strip().str.upper()
        fuel = t["energy source code"].astype(str).str.strip().str.upper()
        t["cat"] = fuel.map(FUEL_CODE).fillna(7).astype(int)
        t.loc[pmc.isin(STORAGE_PM), "cat"] = STORAGE_CAT
        t["stype"] = pmc.map(STORAGE_PM).fillna("")
        t["mw"] = to_num(t["nameplate capacity (mw)"])
        t["mwh"] = to_num(t["nameplate energy capacity (mwh)"]) if "nameplate energy capacity (mwh)" in t else 0.0
        t["lat"], t["lon"] = to_num(t["latitude"]), to_num(t["longitude"])
        t["tech"] = t["technology"].astype(str).str.strip()
        t["status"] = t["status"].astype(str).str.strip().str.upper() if "status" in t else name[:2].upper()
        if name == "Planned":
            t["online"] = yyyymm(t["planned operation year"], t["planned operation month"])
        else:
            t["online"] = yyyymm(t["operating year"], t["operating month"])
        t["retired"] = yyyymm(t["retirement year"], t["retirement month"]) if name == "Retired" else pd.NA
        tabs[name] = t
        print(f"  {path.name}: {name} {len(t):,} units, {t['mw'].sum():,.0f} MW")

    op, ret, pl = tabs.get("Operating"), tabs.get("Retired"), tabs.get("Planned")
    key = ["plant_code", "cat"]
    g_op = op.groupby(key).agg(online_op=("online", "min"), mw=("mw", "sum"), mwh=("mwh", "sum"),
                               n_op=("mw", "size"), tech=("tech", lambda t: t.mode().iat[0] if len(t.mode()) else ""),
                               status=("status", lambda st: "OP" if (st == "OP").any() else st.mode().iat[0]),
                               stype=("stype", lambda t: t.mode().iat[0] if len(t.mode()) else ""))
    g_ret = ret.groupby(key).agg(online_ret=("online", "min"), retired_max=("retired", "max"), n_ret=("mw", "size"),
                                 mw_ret=("mw", "sum"), mwh_ret=("mwh", "sum"),
                                 tech_ret=("tech", lambda t: t.mode().iat[0] if len(t.mode()) else ""),
                                 stype_ret=("stype", lambda t: t.mode().iat[0] if len(t.mode()) else ""))
    tl = g_op.join(g_ret, how="outer")
    tl["online"] = tl[["online_op", "online_ret"]].min(axis=1)
    tl["retired"] = tl["retired_max"].where(tl["n_op"].isna())        # retired only if nothing still operating
    tl["status"] = tl["status"].fillna("RE")
    tl["tech"] = tl["tech"].fillna(tl["tech_ret"])
    tl["stype"] = tl["stype"].fillna(tl["stype_ret"])
    tl["mw"] = tl["mw"].fillna(0.0)
    tl["mwh"] = tl["mwh"].fillna(0.0)
    timeline = tl[["online", "retired", "status", "mw", "mwh", "tech", "stype", "n_op", "n_ret"]]

    allrows = pd.concat([t for t in tabs.values()], ignore_index=True)
    ok = allrows["lat"].between(-90, 90) & allrows["lon"].between(-180, 180) & (allrows["lat"] != 0)
    coords = (allrows[ok].groupby("plant_code").agg(lon=("lon", "first"), lat=("lat", "first"),
                                                     name860=("plant name", "first"), state860=("plant state", "first")))
    coords["lon"], coords["lat"] = coords["lon"].round(5), coords["lat"].round(5)

    planned = None
    if pl is not None:
        planned = pl.groupby(key).agg(mw=("mw", "sum"), mwh=("mwh", "sum"), planned=("online", "min"),
                                      name=("plant name", "first"), lat=("lat", "first"), lon=("lon", "first"),
                                      stype=("stype", lambda t: t.mode().iat[0] if len(t.mode()) else ""),
                                      tech=("tech", lambda t: t.mode().iat[0] if len(t.mode()) else "")).reset_index()
    print(f"  860M timeline: {len(timeline):,} (plant, fuel) entries, {int(timeline['retired'].notna().sum()):,} fully retired, "
          f"{len(coords):,} plant coordinates" + (f", {len(planned):,} planned (plant, fuel)" if planned is not None else ""))
    return timeline, planned, coords


# ----------------------------------------------------------------------------
def early_release_from() -> int:
    """First data year that is still EIA 'early release' (finals arrive ~September of the next year)."""
    import datetime
    t = datetime.date.today()
    return t.year - 1 if t.month >= 10 else t.year - 2


def main() -> None:
    # accept zips, or folders if the browser unzipped them (Safari does by default)
    def find(pattern):
        return sorted(p for p in RAW.glob(pattern) if p.is_dir() or p.suffix == ".zip")
    zips923 = find("f923_*") + find("f906920_*")
    zips860 = find("eia860*")
    if not zips923 or not zips860:
        sys.exit(f"need EIA-923 and EIA-860 zips in {RAW} (run pipeline/download.py)")

    years = sorted(int(re.search(r"_(\d{4})(?:\.zip)?$", z.name).group(1)) for z in zips923)
    print(f"EIA-923 years found: {years[0]}-{years[-1]} ({len(years)} files)")
    print("Loading EIA-860 ...")
    coords, storage = load_860(zips860)
    m860 = newest_860m()
    timeline = planned = None
    if m860 is not None:
        print(f"Loading EIA-860M ({m860.name}) ...")
        timeline, planned, coords_m = load_860m(m860)
        extra = coords_m[~coords_m.index.isin(coords.index)]
        coords = pd.concat([coords, extra])
        print(f"  860M added coordinates for {len(extra):,} plants missing from the annual files")
    else:
        print("  (no EIA-860M file in data/raw - dates/status will come from EIA-923 output only)")

    frames = []
    for zp in zips923:
        year = int(re.search(r"_(\d{4})(?:\.zip)?$", zp.name).group(1))
        frames.append(parse_923(zp, year))
    gen = pd.concat(frames, ignore_index=True)
    mcols = [f"m{i}" for i in range(12)]

    # Coverage check. EIA's in-year monthly file carries only the monthly-sample
    # plants individually (annual filers appear as state-level 99999 estimates,
    # which we drop). A year whose plant count collapses vs the previous year is
    # excluded unless INCLUDE_PARTIAL_YEARS=1, because it would understate small
    # plants (in 2024, annual filers were 27% of wind and 49% of hydro output).
    counts = gen[gen[mcols].sum(axis=1) != 0].groupby("year")["plant_code"].nunique()
    print("plants reporting per year: " + ", ".join(f"{y}: {n:,}" for y, n in counts.items()))
    gen["est"] = False
    sample_years = [y for y in years[1:] if counts.get(y, 0) < 0.6 * counts.get(y - 1, 1)]
    estimated_years = []
    for y in sample_years:
        est_file = CACHE / f"est_{y}.csv"
        if est_file.exists() and not os.environ.get("EXCLUDE_PARTIAL_YEARS"):
            est = pd.read_csv(est_file, dtype={"state": str, "pm": str, "fuel": str})
            gen = pd.concat([gen, impute_year(gen, y, est)], ignore_index=True)
            estimated_years.append(y)
        else:
            print(f"EXCLUDING {y}: plant coverage < 60% of the prior year and no EIA estimates to complete it")
            gen = gen[gen["year"] != y]
            years = [yy for yy in years if yy != y]

    # --- aggregate to (plant, category, year) ---------------------------------
    # Chris's "Other" heuristic: a plant that reports Other AND a real fuel is
    # just mis-classifying some units -> fold Other into its biggest real fuel.
    gen["total"] = gen[mcols].clip(lower=0).sum(axis=1)
    by_cat = gen.groupby(["plant_code", "cat"])["total"].sum().unstack(fill_value=0)
    if 7 in by_cat.columns:
        real = by_cat.drop(columns=[7])
        has_real = real.sum(axis=1) > 0
        fold = real[has_real].idxmax(axis=1)
        remap = gen["plant_code"].map(fold)
        mask = (gen["cat"] == 7) & remap.notna()
        gen.loc[mask, "cat"] = remap[mask].astype(int)
        print(f"folded {int(mask.sum())} 'Other' rows into their plant's main fuel")

    agg = gen.groupby(["plant_code", "cat", "year"], as_index=False)[mcols].sum()
    est_from = gen[gen["est"]].groupby(["plant_code", "cat"])["year"].min() * 100 + 1
    names = gen.sort_values("year").groupby("plant_code")["plant_name"].last()
    stypes = (gen[gen["stype"].fillna("") != ""].groupby("plant_code")["stype"]
              .agg(lambda s: s.mode().iat[0] if len(s.mode()) else "battery"))

    # drop month columns beyond the data (partial final year): detect all-zero trailing months
    last_year = years[-1]
    ly = agg[agg["year"] == last_year]
    last_month = max((i for i in range(12) if ly[f"m{i}"].abs().sum() > 0), default=11)
    months = [(y, m) for y in years for m in range(12) if not (y == last_year and m > last_month)]
    print(f"time axis: {len(months)} months, {years[0]}-01 .. {last_year}-{last_month + 1:02d}")

    # --- join coordinates -----------------------------------------------------
    plants = agg["plant_code"].unique()
    have = coords.index.intersection(plants)
    missing_gen = agg[~agg["plant_code"].isin(have)]
    print(f"plants: {len(plants)}, with coordinates: {len(have)} "
          f"({100 * len(have) / len(plants):.1f}%); generation dropped for lack of coords: "
          f"{100 * missing_gen[mcols].clip(lower=0).sum().sum() / agg[mcols].clip(lower=0).sum().sum():.2f}%")
    top_missing = (missing_gen.assign(t=missing_gen[mcols].clip(lower=0).sum(axis=1))
                   .groupby("plant_code")["t"].sum().sort_values(ascending=False).head(5))
    if len(top_missing):
        print("  biggest plants without coordinates:",
              [(int(c), names.get(c, "?"), f"{v / 1e3:,.0f} GWh") for c, v in top_missing.items()])
    agg = agg[agg["plant_code"].isin(have)]

    # --- build features --------------------------------------------------------
    # Long format (one row per plant, category, year, month with non-zero output)
    # is far faster than iterating rows of the wide table.
    month_set = set(months)
    long = agg.melt(id_vars=["plant_code", "cat", "year"], value_vars=mcols, var_name="m", value_name="v")
    long = long[long["v"] != 0].copy()
    long["m"] = long["m"].str[1:].astype(int)
    long = long[[(y, m) in month_set for y, m in zip(long["year"].values, long["m"].values)]]
    long["v"] = long["v"].round().astype("int64")
    long["ym"] = long["year"] * 100 + long["m"] + 1
    long["q"] = long["m"] // 3
    long = long.sort_values(["plant_code", "cat", "year", "m"])
    feats_m, feats_q, feats_l, conflicts = [], [], [], []
    totals = defaultdict(lambda: defaultdict(float))  # totals[cat][year] MWh
    pos = long[long["v"] > 0]
    for (cat, y), v in pos.groupby(["cat", "year"])["v"].sum().items():
        totals[int(cat)][int(y)] = float(v)
    for (code, cat), grp in long.groupby(["plant_code", "cat"], sort=False):
        lon, lat, name860, _ = coords.loc[code]
        yrs, ms, vs = grp["year"].values, grp["m"].values, grp["v"].values
        monthly = {f"netgen_{y}_{m}": int(v) for y, m, v in zip(yrs, ms, vs)}
        quarterly = {}
        for y, q, v in zip(yrs, ms // 3, vs):
            k = f"netgen_{y}_{q}"
            quarterly[k] = quarterly.get(k, 0) + int(v)
        p = grp[grp["v"] > 0]
        if p.empty:
            continue
        lifetime = float(p["v"].sum())
        first, last = int(p["ym"].min()), int(p["ym"].max())
        fid = f"{code}-{cat}"
        # first/last = YYYYMM of first and last month with output; the UI hides
        # plants before `first` and marks them offline after `last`
        base = {"id": fid, "fuel_type": int(cat), "first": first, "last": last}
        if (code, cat) in est_from.index:
            base["est_from"] = int(est_from.loc[(code, cat)])   # months from here are EIA/state estimates
        if cat == STORAGE_CAT:
            base["stype"] = stypes.get(code, "battery")
        geom = {"type": "Point", "coordinates": [lon, lat]}
        feats_m.append({"type": "Feature", "geometry": geom, "properties": {**base, **monthly}})
        feats_q.append({"type": "Feature", "geometry": geom, "properties": {**base, **quarterly}})
        label = {**base, "name": names.get(code, name860), "netgen": round(lifetime)}
        tl = timeline.loc[(code, cat)] if timeline is not None and (code, cat) in timeline.index else None
        if tl is not None:
            if pd.notna(tl["online"]):
                label["online"] = int(tl["online"])
            if pd.notna(tl["retired"]):
                label["retired"] = int(tl["retired"])
                if last > int(tl["retired"]):
                    conflicts.append((code, cat, label["name"], int(tl["retired"]), last))
            label["status"] = tl["status"]
        if cat == STORAGE_CAT:
            pm_codes = [k for k, v in STORAGE_PM.items() if v == base["stype"]]
            srow = storage[(storage["plant_code"] == code) & (storage["pm"].isin(pm_codes))] if storage is not None else []
            if len(srow):
                label["mw"] = round(float(srow["mw"].sum()), 1)
                label["mwh"] = round(float(srow["mwh"].sum()), 1)
                label["tech"] = srow["tech"].iat[0]
                if pd.notna(srow["op_year"].min()):
                    label["op_year"] = int(srow["op_year"].min())
            elif tl is not None and tl["mw"] > 0:          # e.g. units added in 2026, or retired ones
                label["mw"], label["mwh"] = round(float(tl["mw"]), 1), round(float(tl["mwh"]), 1)
                label["tech"] = str(tl["tech"])
                if "online" in label:
                    label["op_year"] = label["online"] // 100
        feats_l.append({"type": "Feature", "geometry": geom, "properties": label})

    # Biggest plants first so clusters tend to sit on the biggest plant
    order = sorted(range(len(feats_l)), key=lambda i: -feats_l[i]["properties"]["netgen"])
    feats_m, feats_q, feats_l = [[f[i] for i in order] for f in (feats_m, feats_q, feats_l)]

    for fn, feats in (("plant_generation", feats_m), ("quarterly_generation", feats_q), ("plant_labels", feats_l)):
        path = OUT / f"{fn}.geojson"
        with open(path, "w") as f:
            json.dump({"type": "FeatureCollection", "features": feats}, f, separators=(",", ":"))
        print(f"wrote {path.name}: {len(feats)} features, {path.stat().st_size / 1e6:.1f} MB")

    if conflicts:
        qa = pd.DataFrame(conflicts, columns=["plant_code", "fuel_type", "name", "retired_860m", "last_output_923"])
        qa.to_csv(OUT / "qa_output_after_retirement.csv", index=False)
        print(f"QA: {len(qa)} plant-fuel features reported output after their 860M retirement date "
              f"(923 wins; list in qa_output_after_retirement.csv)")

    if planned is not None:
        pf = []
        for _, r in planned.iterrows():
            if not (-90 < r["lat"] < 90 and -180 < r["lon"] < 180) or r["mw"] <= 0:
                continue
            props = {"id": f"{int(r['plant_code'])}-{int(r['cat'])}-p", "fuel_type": int(r["cat"]), "name": str(r["name"]),
                     "mw": round(float(r["mw"]), 1), "mwh": round(float(r["mwh"]), 1)}
            if pd.notna(r["planned"]):
                props["planned"] = int(r["planned"])
            if int(r["cat"]) == STORAGE_CAT:
                props["stype"] = r["stype"] or "battery"
            pf.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(float(r["lon"]), 5), round(float(r["lat"]), 5)]},
                       "properties": props})
        with open(OUT / "planned.geojson", "w") as f:
            json.dump({"type": "FeatureCollection", "features": pf}, f, separators=(",", ":"))
        pmw = planned[planned["cat"] == STORAGE_CAT]["mw"].sum()
        print(f"wrote planned.geojson: {len(pf)} planned (plant, fuel) units; planned storage {pmw:,.0f} MW")

    # --- totals / normalisation ------------------------------------------------
    ref_year = max(y for y in years if y <= last_year - (1 if last_month < 11 else 0))  # last complete year
    annual = pd.DataFrame({FUEL_NAMES[c]: {y: totals[c].get(y, 0) / 1e6 for y in years} for c in sorted(totals)})
    annual.index.name = "year"
    annual.round(1).to_csv(OUT / "annual_twh.csv")
    fuel_totals = {
        "years": years,
        "months": [f"{y}_{m}" for y, m in months],
        "quarters": sorted({f"{y}_{m // 3}" for y, m in months}, key=lambda s: tuple(map(int, s.split("_")))),
        "reference_year": ref_year,
        "fuel_names": FUEL_NAMES,
        "twh_by_fuel_reference_year": {str(c): round(totals[c].get(ref_year, 0) / 1e6, 1) for c in sorted(totals)},
        "twh_all_generation_reference_year": round(sum(totals[c].get(ref_year, 0) for c in totals if c != STORAGE_CAT) / 1e6, 1),
        "storage_data_start_year": 2014,
        "partial_final_year": last_month < 11,
        "early_release_years": [y for y in years if y >= early_release_from()],
        "estimated_years": estimated_years,
        "eia860m_file": m860.name if m860 else None,
        "has_planned": planned is not None,
    }
    with open(OUT / "fuel_totals.json", "w") as f:
        json.dump(fuel_totals, f, indent=1)
    print("\nAnnual TWh by fuel (sanity check against EIA published totals):")
    pd.set_option("display.width", 200)
    print(annual.round(0).astype(int).to_string())


if __name__ == "__main__":
    main()
