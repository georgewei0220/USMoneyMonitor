#!/usr/bin/env python3
"""US liquidity / funding stress monitor, v1.

Two commands:
  python monitor.py update                      fetch data, store in SQLite
  python monitor.py study --t0 2026-11-12       event study around a date

What it does NOT do: it does not see real fund flows. It watches price- and
rate-based stress proxies (credit spreads, funding rates, Treasury yields,
volatility, equities) and asks: around T0, which of them moved unusually,
compared with their own last five years of 10-day moves?

Data sources
  FRED   : https://fred.stlouisfed.org/graph/fredgraph.csv?id=<CODE>  (no API key)
  Yahoo  : via the yfinance package
Series codes below are from memory; if one returns no data, check the code on
fred.stlouisfed.org and fix it in SERIES.
"""
import argparse
import io
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import requests

DEFAULT_DB = Path(__file__).with_name("monitor.db")
START = "2020-01-01"

# name: (source, code, kind, stress_dir, label)
#   kind        "level": change measured in units (spreads, yields, indices of stress)
#               "price": change measured in percent
#   stress_dir  +1 rising means stress, -1 falling means stress, 0 ambiguous
SERIES = {
    "HY_OAS":   ("fred",  "BAMLH0A0HYM2", "level", +1, "High-yield OAS (%)"),
    "IG_OAS":   ("fred",  "BAMLC0A0CM",   "level", +1, "Inv-grade OAS (%)"),
    "UST2Y":    ("fred",  "DGS2",         "level",  0, "2Y Treasury yield (%)"),
    "UST10Y":   ("fred",  "DGS10",        "level",  0, "10Y Treasury yield (%)"),
    "CURVE":    ("fred",  "T10Y2Y",       "level",  0, "10Y-2Y spread (%)"),
    "SOFR":     ("fred",  "SOFR",         "level",  0, "SOFR (%)"),
    "RRP":      ("fred",  "RRPONTSYD",    "level",  0, "ON RRP usage ($bn)"),
    "TGA":      ("fred",  "WTREGEN",      "level",  0, "Treasury General Acct (weekly)"),
    "RESERVES": ("fred",  "WRESBAL",      "level",  0, "Bank reserves (weekly)"),
    "NFCI":     ("fred",  "NFCI",         "level", +1, "Chicago Fed NFCI (weekly)"),
    "VIX":      ("fred",  "VIXCLS",       "level", +1, "VIX"),
    "MOVE":     ("yahoo", "^MOVE",        "price", +1, "MOVE index"),
    "NVDA":     ("yahoo", "NVDA",         "price", -1, "NVDA"),
    "SOX":      ("yahoo", "^SOX",         "price", -1, "SOX (semis)"),
    "SPX":      ("yahoo", "^GSPC",        "price", -1, "S&P 500"),
    # cash parked on the sidelines (weekly, $bn; measured as % change)
    "MMF_RETAIL": ("fred", "WRMFNS",      "price",  0, "Retail money funds ($bn, weekly)"),
    "DEPOSITS":   ("fred", "DPSACBW027SBOG", "price", 0, "Bank deposits ($bn, weekly)"),
}

# Asset-class ETF prices: stored by `update` and used by the dashboard for
# rotation ratios; not part of the event-study table.
ETFS = {
    "SPY": "S&P 500", "QQQ": "Nasdaq 100", "SMH": "Semiconductors",
    "XLK": "Tech sector", "XLU": "Utilities sector",
    "TLT": "20Y+ Treasuries", "IEF": "7-10Y Treasuries", "SHV": "Short Treasuries (cash-like)",
    "HYG": "High-yield bonds", "LQD": "Inv-grade bonds", "GLD": "Gold", "UUP": "US dollar",
}
ALL_FETCH = {**SERIES, **{f"ETF_{k}": ("yahoo", k, "price", 0, v) for k, v in ETFS.items()}}

LOOKBACK_OBS = 252 * 5   # history used to rank the event-window move
MIN_HISTORY = 60
EXTREME_PCT = 95         # flag moves in the top/bottom 5% of history (15 series: expect ~1 false flag)


# ----------------------------------------------------------------- storage
def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS obs ("
        "series TEXT NOT NULL, date TEXT NOT NULL, value REAL NOT NULL, "
        "PRIMARY KEY (series, date))"
    )
    return conn


def upsert(conn, name, df):
    rows = [(name, d.strftime("%Y-%m-%d"), float(v)) for d, v in df.items()]
    conn.executemany("INSERT OR REPLACE INTO obs VALUES (?, ?, ?)", rows)
    conn.commit()
    return len(rows)


# ----------------------------------------------------------------- fetching
def fetch_fred(code):
    r = requests.get(
        "https://fred.stlouisfed.org/graph/fredgraph.csv",
        params={"id": code, "cosd": START},
        timeout=30,
    )
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    dates = pd.to_datetime(df.iloc[:, 0])
    values = pd.to_numeric(df.iloc[:, 1], errors="coerce")  # FRED uses "." for missing
    s = pd.Series(values.values, index=dates).dropna()
    return s


def fetch_yahoo(code):
    import yfinance as yf  # imported lazily so FRED-only use does not need it

    df = yf.download(code, start=START, auto_adjust=True, progress=False)
    if df is None or df.empty:
        return pd.Series(dtype=float)
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.dropna()
    close.index = pd.to_datetime(close.index).tz_localize(None)
    return close


def cmd_update(args):
    conn = connect(args.db)
    failed = []
    for name, (source, code, _kind, _dir, _label) in ALL_FETCH.items():
        try:
            s = fetch_fred(code) if source == "fred" else fetch_yahoo(code)
            if s.empty:
                raise ValueError("no data returned")
            n = upsert(conn, name, s)
            print(f"{name:9s} {source:5s} {code:14s} {n:6d} rows, last {s.index[-1].date()}")
        except Exception as e:  # keep going: one bad series should not stop the rest
            failed.append(name)
            print(f"{name:9s} {source:5s} {code:14s} FAILED: {e}", file=sys.stderr)
    conn.close()
    if failed:
        print(f"\nFailed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


# ----------------------------------------------------------------- study
def load_wide(db_path):
    conn = connect(db_path)
    df = pd.read_sql_query("SELECT series, date, value FROM obs", conn)
    conn.close()
    if df.empty:
        sys.exit("Database is empty. Run `python monitor.py update` first.")
    df["date"] = pd.to_datetime(df["date"])
    wide = df.pivot(index="date", columns="series", values="value").sort_index()
    # Use US equity trading days as the calendar; carry weekly series forward.
    if "SPX" in wide and wide["SPX"].notna().any():
        calendar = wide["SPX"].dropna().index
    else:
        calendar = wide.index
    return wide.reindex(calendar.union(wide.index)).ffill(limit=7).reindex(calendar)


def change(x, base, kind):
    """Change of x relative to base, in the series' own measure."""
    return (x / base - 1.0) * 100.0 if kind == "price" else x - base


def event_table(wide, t0, pre, post):
    """Event-study numbers. Returns (table, paths, i0, start, end). Raises ValueError."""
    t0 = pd.Timestamp(t0)
    i0 = int(wide.index.searchsorted(t0, side="right")) - 1
    if i0 < 0:
        raise ValueError(f"No data on or before {t0.date()}.")
    start = i0 - pre
    end = min(i0 + post, len(wide) - 1)
    if start < 0:
        raise ValueError("Not enough history before T0.")

    rows, paths = [], {}
    for name, (_src, _code, kind, sdir, label) in SERIES.items():
        if name not in wide:
            continue
        x = wide[name]
        base = x.iloc[start]
        if pd.isna(base) or pd.isna(x.iloc[i0]):
            continue
        paths[name] = [change(x.iloc[i], base, kind) for i in range(start, end + 1)]
        event = change(x.iloc[i0], base, kind)
        after = change(x.iloc[end], x.iloc[i0], kind) if end > i0 else float("nan")

        hist = x.iloc[max(0, start - LOOKBACK_OBS): start + 1].dropna()  # ends before the window
        if kind == "price":
            moves = hist.pct_change(pre).dropna() * 100.0
        else:
            moves = hist.diff(pre).dropna()
        pct = float((moves <= event).mean() * 100.0) if len(moves) >= MIN_HISTORY else float("nan")

        flag = ""
        if pct == pct:
            if (sdir > 0 and pct >= EXTREME_PCT) or (sdir < 0 and pct <= 100 - EXTREME_PCT):
                flag = "STRESS"
            elif pct >= EXTREME_PCT or pct <= 100 - EXTREME_PCT:
                flag = "extreme"
        rows.append({
            "indicator": name,
            "label": label,
            f"T-{pre}": base,
            "T0": x.iloc[i0],
            "chg_to_T0": event,
            "pctile_vs_5y": pct,
            "chg_after_T0": after,
            "flag": flag,
        })
    return pd.DataFrame(rows), paths, i0, start, end


def cmd_study(args):
    wide = load_wide(args.db)
    try:
        table, paths, i0, start, end = event_table(wide, args.t0, args.pre, args.post)
    except ValueError as e:
        sys.exit(str(e))
    print(f"T0 = {wide.index[i0].date()}  window = {wide.index[start].date()} .. "
          f"{wide.index[end].date()}  ({end - i0} trading days after T0 in the data)\n")

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    print(table.round(3).to_string(index=False))
    print("\nchg_to_T0: units for rate/spread series, percent for prices."
          "\npctile_vs_5y: where this window's move ranks among all earlier "
          f"{args.pre}-day moves (0 = biggest fall, 100 = biggest rise)."
          "\nSTRESS = extreme move in the stress direction; extreme = extreme but direction ambiguous.")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"event_{wide.index[i0].date()}"
    table.to_csv(out / f"{stem}.csv", index=False)
    png = out / f"{stem}.png"
    draw_chart(paths, args, png)
    print(f"\nSaved {out / (stem + '.csv')} and {png}")
    return 0


def draw_chart(paths, args, png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    names = list(paths)
    cols = 4
    rows = -(-len(names) // cols)
    fig, axes = plt.subplots(rows, cols, figsize=(4 * cols, 2.6 * rows), squeeze=False)
    offsets = list(range(-args.pre, -args.pre + len(next(iter(paths.values())))))
    for ax in axes.ravel():
        ax.set_visible(False)
    for ax, name in zip(axes.ravel(), names):
        ax.set_visible(True)
        kind, label = SERIES[name][2], SERIES[name][4]
        ax.plot(offsets, paths[name], marker="o", ms=3, lw=1.2)
        ax.axvline(0, color="red", lw=1, ls="--")
        ax.axhline(0, color="gray", lw=0.6)
        ax.set_title(name, fontsize=10)
        ax.set_ylabel("% change" if kind == "price" else "change (units)", fontsize=8)
        ax.tick_params(labelsize=8)
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    fig.suptitle(f"Change vs T-{args.pre} (trading days relative to T0; red = T0)", fontsize=12)
    fig.tight_layout()
    fig.savefig(png, dpi=130)
    plt.close(fig)


# ----------------------------------------------------------------- CLI
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file (default: monitor.db next to this script)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update", help="fetch all series into the database")
    st = sub.add_parser("study", help="event study around a date")
    st.add_argument("--t0", required=True, help="event date, YYYY-MM-DD")
    st.add_argument("--pre", type=int, default=10, help="trading days before T0 (default 10)")
    st.add_argument("--post", type=int, default=10, help="trading days after T0 (default 10)")
    st.add_argument("--out", default="out", help="output folder (default: out)")
    args = p.parse_args()
    return cmd_update(args) if args.cmd == "update" else cmd_study(args)


if __name__ == "__main__":
    sys.exit(main())
