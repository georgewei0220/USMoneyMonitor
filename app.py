"""美國資金流向觀察 — Streamlit 儀表板

執行:  streamlit run app.py
資料來自 monitor.py 寫入的 SQLite (monitor.db)，可在側邊欄按「更新資料」重新抓取。

注意: 這裡看到的是「價格與利率類的代理指標」，不是真實的基金申贖資金流量。
"""
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import monitor
from glossary import ETF_INFO, EVENT_HELP, HOW_TO_READ, IND, RATIO_INFO, cn_name

st.set_page_config(page_title="美國資金流向觀察", page_icon="📊", layout="wide")


def explain(keys, title="📖 指標說明（點開查看）"):
    """把一組指標的中文說明放進可展開區塊。"""
    with st.expander(title):
        for k in keys:
            if k not in IND:
                continue
            d = IND[k]
            st.markdown(f"**{d['name']}**（`{k}`）\n\n- 是什麼：{d['what']}\n- 怎麼讀：{d['read']}\n- 壓力方向：{d['dir']}")


def explain_etfs(keys, title="📖 資產說明（點開查看）"):
    with st.expander(title):
        for k in keys:
            if k in ETF_INFO:
                n, d = ETF_INFO[k]
                st.markdown(f"- **{k}　{n}**：{d}")

DB = monitor.DEFAULT_DB

# ---------------------------------------------------------------- 資料載入
@st.cache_data(show_spinner=False)
def load_raw(db_path: str, mtime: float) -> pd.DataFrame:
    """原始長表轉寬表，不補值（週頻序列保持原樣）。mtime 只用來讓快取在資料更新後失效。"""
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("SELECT series, date, value FROM obs", conn)
    conn.close()
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot(index="date", columns="series", values="value").sort_index()


@st.cache_data(show_spinner=False)
def load_calendar(db_path: str, mtime: float) -> pd.DataFrame:
    """事件研究用: 以 SPX 交易日為日曆、週頻補值（與 monitor.py 相同）。"""
    return monitor.load_wide(db_path)


def db_ready() -> bool:
    return Path(DB).exists() and Path(DB).stat().st_size > 0


# ---------------------------------------------------------------- 側邊欄
st.sidebar.title("📊 資金流向觀察")

if st.sidebar.button("🔄 更新資料", width="stretch"):
    import argparse

    with st.spinner("正在從 FRED 與 Yahoo 抓取資料…（約 1 分鐘）"):
        rc = monitor.cmd_update(argparse.Namespace(db=str(DB)))
    st.cache_data.clear()
    if rc:
        st.sidebar.warning("部分序列抓取失敗，請看終端機訊息。")
    else:
        st.sidebar.success("更新完成")

if not db_ready():
    st.title("美國資金流向觀察")
    st.info("資料庫還是空的。請先按左側「🔄 更新資料」，或在終端機執行 `python monitor.py update`。")
    st.stop()

raw = load_raw(str(DB), Path(DB).stat().st_mtime)

RANGES = {"3 個月": 63, "6 個月": 126, "1 年": 252, "3 年": 756, "全部": 0}
rng_label = st.sidebar.selectbox("圖表區間", list(RANGES), index=2)
rng_n = RANGES[rng_label]

st.sidebar.caption(
    "資料來源: FRED、Yahoo Finance。\n\n"
    "這是價格／利率類的壓力與輪動代理指標，看不到真實的基金申贖流量。"
)


def window(s: pd.Series) -> pd.Series:
    """依側邊欄選擇截取最近 N 個『交易日』（以 SPY 日曆為準）。"""
    s = s.dropna()
    if rng_n == 0 or "ETF_SPY" not in raw:
        return s
    cutoff = raw["ETF_SPY"].dropna().index[-rng_n]
    return s[s.index >= cutoff]


def ser(name: str) -> pd.Series:
    return raw[name].dropna() if name in raw else pd.Series(dtype=float)


def last_change(s: pd.Series, n: int, pct: bool):
    """最後一筆與 n 筆之前的變動。"""
    s = s.dropna()
    if len(s) <= n:
        return None, None
    new, old = s.iloc[-1], s.iloc[-1 - n]
    return new, ((new / old - 1) * 100 if pct else new - old)


# ---------------------------------------------------------------- 標頭
st.title("美國資金流向觀察")
latest = raw["ETF_SPY"].dropna().index[-1] if "ETF_SPY" in raw else raw.index[-1]
st.caption(f"市場資料最新日期: **{latest.date()}**　｜　週頻資料(基金、存款、準備金等)會落後數日")

tab_over, tab_rot, tab_cash, tab_stress, tab_event = st.tabs(
    ["總覽", "短期: 資金輪動", "中期: 現金與流動性", "壓力指標", "事件研究"]
)

# ================================================================ 總覽
with tab_over:
    with st.expander("📌 第一次使用？先看這裡：這個頁面怎麼讀", expanded=False):
        st.markdown(HOW_TO_READ)
    st.subheader("關鍵指標（近 10 個交易日變動）")
    # (series, 標題, 單位格式, 是否用百分比變動, 上升是否為壓力: +1/-1/0)
    cards = [
        ("ETF_SPY", "S&P 500 (SPY)", "{:,.1f}", True, -1),
        ("NVDA", "NVDA", "{:,.1f}", True, -1),
        ("SOX", "費半 SOX", "{:,.0f}", True, -1),
        ("UST10Y", "10Y 殖利率 %", "{:.2f}", False, 0),
        ("HY_OAS", "高收益債利差 %", "{:.2f}", False, +1),
        ("VIX", "VIX", "{:.1f}", False, +1),
        ("MMF_RETAIL", "零售貨幣基金 $bn", "{:,.0f}", True, 0),
        ("DEPOSITS", "銀行存款 $bn", "{:,.0f}", True, 0),
    ]
    cols = st.columns(4)
    for i, (name, title, fmt, pct, sdir) in enumerate(cards):
        s = ser(name)
        n = 2 if name in ("MMF_RETAIL", "DEPOSITS") else 10  # 週頻用 2 週
        val, chg = last_change(s, n, pct)
        info = IND.get("SPX" if name == "ETF_SPY" else name)
        tip = f"{info['what']}\n\n怎麼讀：{info['read']}" if info else None
        with cols[i % 4]:
            if val is None:
                st.metric(title, "—", help=tip)
            else:
                delta = f"{chg:+.2f}%" if pct else f"{chg:+.2f}"
                color = "off" if sdir == 0 else ("inverse" if sdir > 0 else "normal")
                st.metric(title, fmt.format(val), delta, delta_color=color, help=tip)
    st.caption("把滑鼠移到卡片右上角的 ? 可看說明。週頻序列（貨幣基金、存款）的變動為近 2 週；"
               "數字下方的箭頭：紅色＝往壓力方向，綠色＝往反方向，灰色＝方向不固定。")
    explain(["SPX", "NVDA", "SOX", "UST10Y", "HY_OAS", "VIX", "MMF_RETAIL", "DEPOSITS"],
            "📖 這 8 個關鍵指標是什麼？")

    st.subheader("資金往哪邊走？ 各資產近期報酬")
    assets = {k: f"{k} {v}" for k, v in monitor.ETFS.items()}
    rows = []
    for k, label in assets.items():
        s = ser(f"ETF_{k}")
        if len(s) < 61:
            continue
        rows.append({
            "資產": label,
            "5 日 %": (s.iloc[-1] / s.iloc[-6] - 1) * 100,
            "20 日 %": (s.iloc[-1] / s.iloc[-21] - 1) * 100,
            "60 日 %": (s.iloc[-1] / s.iloc[-61] - 1) * 100,
        })
    perf = pd.DataFrame(rows).set_index("資產")
    fig = go.Figure(go.Heatmap(
        z=perf.values, x=perf.columns, y=perf.index,
        colorscale="RdYlGn", zmid=0,
        text=[[f"{v:+.1f}%" for v in r] for r in perf.values],
        texttemplate="%{text}", hovertemplate="%{y}<br>%{x}: %{z:+.2f}%<extra></extra>",
    ))
    fig.update_layout(height=60 + 38 * len(perf), margin=dict(l=0, r=0, t=10, b=0),
                      yaxis=dict(autorange="reversed"))
    st.plotly_chart(fig, width="stretch")
    st.caption("綠色＝上漲、紅色＝下跌。同一時間哪些資產綠、哪些紅，就是資金目前偏好與迴避的方向"
               "（價格面推論）。例如股票紅、短債與長債綠，通常代表偏向避險。")
    explain_etfs(list(monitor.ETFS))

# ================================================================ 短期: 資金輪動
with tab_rot:
    st.subheader("各資產走勢（區間起點 = 100）")
    default = ["SPY", "QQQ", "SMH", "TLT", "SHV", "HYG", "GLD"]
    pick = st.multiselect("選擇資產", list(monitor.ETFS), default=default,
                          format_func=lambda k: f"{k} {monitor.ETFS[k]}")
    fig = go.Figure()
    for k in pick:
        s = window(ser(f"ETF_{k}"))
        if s.empty:
            continue
        fig.add_trace(go.Scatter(x=s.index, y=s / s.iloc[0] * 100, name=k, mode="lines"))
    fig.add_hline(y=100, line_color="gray", line_width=0.6)
    fig.update_layout(height=420, margin=dict(l=0, r=0, t=10, b=0), hovermode="x unified",
                      legend=dict(orientation="h"))
    st.plotly_chart(fig, width="stretch")
    st.caption("所有資產都換算成區間起點 = 100，方便比較『誰漲得多、誰跌得多』，與價格高低無關。")
    explain_etfs(list(monitor.ETFS))

    st.subheader("輪動比價")
    st.caption("比值上升＝分子相對分母較強。上升代表資金偏向分子那一邊（價格面推論，不是實際申贖）。")
    ratios = [
        ("QQQ / SPY", "ETF_QQQ", "ETF_SPY", "成長股 vs 大盤"),
        ("SMH / SPY", "ETF_SMH", "ETF_SPY", "半導體 vs 大盤"),
        ("XLK / XLU", "ETF_XLK", "ETF_XLU", "科技 vs 防禦(公用事業)"),
        ("SPY / TLT", "ETF_SPY", "ETF_TLT", "股票 vs 長債"),
        ("HYG / LQD", "ETF_HYG", "ETF_LQD", "高收益 vs 投資級(風險偏好)"),
        ("SPY / SHV", "ETF_SPY", "ETF_SHV", "股票 vs 類現金"),
    ]
    fig = make_subplots(rows=2, cols=3, subplot_titles=[f"{r[0]}　{r[3]}" for r in ratios],
                        vertical_spacing=0.16)
    summary = []
    for i, (title, a, b, desc) in enumerate(ratios):
        r_all = (ser(a) / ser(b)).dropna()
        r = window(r_all)
        if r.empty:
            continue
        ma = r_all.rolling(50).mean().reindex(r.index)
        fig.add_trace(go.Scatter(x=r.index, y=r, mode="lines", showlegend=False,
                                 line=dict(width=1.5)), row=i // 3 + 1, col=i % 3 + 1)
        fig.add_trace(go.Scatter(x=ma.index, y=ma, mode="lines", showlegend=False,
                                 line=dict(width=1, dash="dot", color="gray")),
                      row=i // 3 + 1, col=i % 3 + 1)
        chg20 = (r_all.iloc[-1] / r_all.iloc[-21] - 1) * 100 if len(r_all) > 21 else float("nan")
        above = r_all.iloc[-1] > r_all.rolling(50).mean().iloc[-1]
        reading = RATIO_INFO[title][1] if above else RATIO_INFO[title][2]
        summary.append({"比價": title, "意義": desc, "最新": round(r_all.iloc[-1], 3),
                        "20 日變動 %": round(chg20, 2), "高於 50 日均線": "是" if above else "否",
                        "目前解讀": reading})
    fig.update_layout(height=520, margin=dict(l=0, r=0, t=40, b=0))
    fig.update_annotations(font_size=12)
    st.plotly_chart(fig, width="stretch")
    st.caption("實線為比值，虛線為 50 日均線。比值在均線之上，代表近期趨勢偏向分子；在均線之下則偏向分母。")
    st.dataframe(pd.DataFrame(summary), hide_index=True, width="stretch")
    st.caption("「目前解讀」只是依比值與 50 日均線的位置所做的簡單判斷，不是預測，也不代表有實際資金移動。")
    with st.expander("📖 6 組比價分別代表什麼？"):
        for t, (d, up, down) in RATIO_INFO.items():
            st.markdown(f"**{t}**（{d}）\n\n- {up}\n- {down}")

# ================================================================ 中期: 現金與流動性
with tab_cash:
    st.subheader("場邊現金: 貨幣基金與銀行存款")
    c1, c2 = st.columns(2)
    for col, name, title in [(c1, "MMF_RETAIL", "零售貨幣基金規模 ($bn)"),
                             (c2, "DEPOSITS", "商業銀行存款 ($bn)")]:
        s = ser(name)
        if rng_n:
            s = s[s.index >= s.index[-1] - pd.Timedelta(days=int(rng_n * 1.45))]
        with col:
            fig = go.Figure(go.Scatter(x=s.index, y=s, mode="lines"))
            fig.update_layout(title=title, height=300, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, width="stretch")
    st.caption("貨幣基金規模持續上升常被解讀為資金停泊在場邊，但也可能只是短天期利率較高所吸引。"
               "FRED 上的機構貨幣基金序列已在 2021 年停止更新，故這裡只有零售部分。")

    st.subheader("聯準會體系的流動性: 準備金、TGA、隔夜逆回購")
    fig = make_subplots(rows=1, cols=3, subplot_titles=["銀行準備金 ($bn)", "財政部帳戶 TGA ($bn)", "隔夜逆回購 RRP ($bn)"])
    for i, (name, scale) in enumerate([("RESERVES", 1000), ("TGA", 1000), ("RRP", 1)]):
        s = ser(name)
        if rng_n:
            s = s[s.index >= s.index[-1] - pd.Timedelta(days=int(rng_n * 1.45))]
        fig.add_trace(go.Scatter(x=s.index, y=s / scale, mode="lines", showlegend=False),
                      row=1, col=i + 1)
    fig.update_layout(height=300, margin=dict(l=0, r=0, t=40, b=0))
    st.plotly_chart(fig, width="stretch")
    st.caption("準備金與 TGA 原始單位為百萬美元，此處已換算為十億美元。"
               "財政部多發債、TGA 上升，通常會從銀行體系抽走準備金。")

    st.subheader("近期變動")
    rows = []
    for name, title, n in [("MMF_RETAIL", "零售貨幣基金", 4), ("DEPOSITS", "銀行存款", 4),
                           ("RESERVES", "銀行準備金", 4), ("TGA", "TGA", 4)]:
        s = ser(name)
        if len(s) > n:
            rows.append({"項目": title, f"最新({s.index[-1].date()})": round(s.iloc[-1], 1),
                         f"近 {n} 週變動 %": round((s.iloc[-1] / s.iloc[-1 - n] - 1) * 100, 2)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    explain(["MMF_RETAIL", "DEPOSITS", "RESERVES", "TGA", "RRP"])

# ================================================================ 壓力指標
with tab_stress:
    st.subheader("信用、利率、波動")
    panels = [
        ("HY_OAS", "高收益債利差 (%)"), ("IG_OAS", "投資級債利差 (%)"),
        ("UST10Y", "10Y 殖利率 (%)"), ("CURVE", "10Y-2Y 利差 (%)"),
        ("VIX", "VIX"), ("MOVE", "MOVE 債市波動"),
        ("SOFR", "SOFR (%)"), ("NFCI", "芝加哥聯儲 NFCI"),
    ]
    fig = make_subplots(rows=2, cols=4, subplot_titles=[p[1] for p in panels], vertical_spacing=0.18)
    for i, (name, _t) in enumerate(panels):
        s = window(ser(name))
        fig.add_trace(go.Scatter(x=s.index, y=s, mode="lines", showlegend=False),
                      row=i // 4 + 1, col=i % 4 + 1)
    fig.update_layout(height=480, margin=dict(l=0, r=0, t=40, b=0))
    fig.update_annotations(font_size=12)
    st.plotly_chart(fig, width="stretch")
    st.caption("利差與 VIX 同步大幅上升，才是比較明確的流動性緊縮訊號；單一指標波動不代表系統性壓力。")
    explain([p[0] for p in panels])

# ================================================================ 事件研究
with tab_event:
    st.subheader("事件研究: 某一天前後，哪些指標異常？")
    with st.expander("📖 這個表怎麼讀？（建議先看）"):
        st.markdown(EVENT_HELP)
    wide = load_calendar(str(DB), Path(DB).stat().st_mtime)
    c1, c2, c3 = st.columns(3)
    t0 = c1.date_input("事件日 T0", value=wide.index[-1].date(),
                       min_value=wide.index[0].date(), max_value=wide.index[-1].date())
    pre = c2.number_input("T0 之前的交易日數", 3, 60, 10)
    post = c3.number_input("T0 之後的交易日數", 0, 60, 10)

    try:
        table, paths, i0, start, end = monitor.event_table(wide, t0, int(pre), int(post))
    except ValueError as e:
        st.error(str(e))
        st.stop()

    st.write(f"T0 = **{wide.index[i0].date()}**　區間 {wide.index[start].date()} → {wide.index[end].date()}"
             f"（T0 之後資料中有 {end - i0} 個交易日）")

    flagged = table[table["flag"] != ""]
    if flagged.empty:
        st.success("沒有指標落在歷史前後 5% 的極端區間。")
    else:
        for _, r in flagged.iterrows():
            icon = "🔴" if r["flag"] == "STRESS" else "🟠"
            st.write(f"{icon} **{cn_name(r['indicator'])}**（{r['indicator']}）: 變動 {r['chg_to_T0']:+.2f}，"
                     f"位於過去 5 年 {r['pctile_vs_5y']:.1f} 百分位 — {r['flag']}")

    table = table.copy()
    table.insert(1, "中文名稱", table["indicator"].map(cn_name))
    table["壓力方向"] = table["indicator"].map(lambda k: IND[k]["dir"].split("（")[0] if k in IND else "")
    show = table.drop(columns=["label"]).rename(columns={
        "indicator": "指標", "chg_to_T0": "到 T0 變動",
        "pctile_vs_5y": "5 年百分位", "chg_after_T0": "T0 後變動", "flag": "旗標"})
    st.dataframe(show.round(3), hide_index=True, width="stretch")
    explain(list(monitor.SERIES), "📖 每個指標是什麼？（共 18 個，點開查看）")
    st.caption("到 T0 變動: 利率/利差類為絕對值，價格與基金類為百分比。"
               "百分位 0 = 歷史上最大跌幅，100 = 最大漲幅。"
               "STRESS = 往壓力方向的極端變動；extreme = 極端但方向不明。"
               "約 15 個指標用 5% 門檻，平均會有 1 個假警報。")

    st.subheader("T-前後走勢（相對 T-起點的變化）")
    names = list(paths)
    ncol = 4
    nrow = -(-len(names) // ncol)
    offs = list(range(-int(pre), -int(pre) + len(next(iter(paths.values())))))
    fig = make_subplots(rows=nrow, cols=ncol, subplot_titles=names, vertical_spacing=0.09)
    for i, name in enumerate(names):
        fig.add_trace(go.Scatter(x=offs, y=paths[name], mode="lines+markers", showlegend=False,
                                 marker=dict(size=4)), row=i // ncol + 1, col=i % ncol + 1)
        fig.add_vline(x=0, line_color="red", line_dash="dash", line_width=1,
                      row=i // ncol + 1, col=i % ncol + 1)
    fig.update_layout(height=190 * nrow, margin=dict(l=0, r=0, t=30, b=0))
    fig.update_annotations(font_size=11)
    st.plotly_chart(fig, width="stretch")
