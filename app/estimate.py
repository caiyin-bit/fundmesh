"""盘中估值引擎。

官方估值接口已于 2024 年全行业下架，只能自建。两种机制按可靠性分层：
  1. 代理标的：资产设了 proxy_code（指数或场内 ETF）时，直接用它的实时涨跌幅
  2. 重仓股加权：Σ(个股占净值比 × 个股实时涨跌幅)，未覆盖部分视为不动

估算值一律标注覆盖率，收盘后由官方净值覆盖。债券/货币/QDII 不做盘中估值。
"""

import time
from datetime import date, datetime, time as Time, timedelta, timezone

import akshare as ak

from app.data import SESSION, _cached

CN_TZ = timezone(timedelta(hours=8))
MORNING = (Time(9, 30), Time(11, 30))
AFTERNOON = (Time(13, 0), Time(15, 0))

# 这些类型盘中估值没意义或算不出：债券波动小、货基恒定、境外持仓拿不到 A 股行情
SKIP_KEYWORDS = ("债券", "货币", "QDII", "海外", "港股", "美元", "黄金")


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


# ---------- 交易日历 ----------

_cal: set[date] = set()
_cal_at: float = 0.0


def sync_trade_calendar() -> int:
    """同步 A 股交易日历入库（含节假日调休）。幂等，返回入库总数。"""
    from app.db import pool

    df = ak.tool_trade_date_hist_sina()
    days = [(d,) for d in df["trade_date"]]
    with pool.connection() as conn:
        conn.cursor().executemany(
            "INSERT INTO trade_calendar(date) VALUES(%s) ON CONFLICT DO NOTHING", days
        )
    return len(days)


def trade_days() -> set[date]:
    """交易日集合。内存缓存 1 天；库里缺失或未覆盖今年则先同步。"""
    global _cal, _cal_at
    from app.db import pool

    if _cal and time.time() - _cal_at < 86400:
        return _cal
    with pool.connection() as conn:
        latest = conn.execute("SELECT MAX(date) AS d FROM trade_calendar").fetchone()["d"]
        if latest is None or latest < now_cn().date():
            try:
                sync_trade_calendar()
            except Exception:
                pass                      # 拉取失败则用库里已有的，实在没有就退化为按周判断
        rows = conn.execute("SELECT date FROM trade_calendar").fetchall()
    _cal, _cal_at = {r["date"] for r in rows}, time.time()
    return _cal


def is_trading_day(d: date) -> bool:
    cal = trade_days()
    if not cal:
        return d.weekday() < 5            # 日历不可用时的退化判断
    return d in cal


def is_trading_now() -> bool:
    n = now_cn()
    if not is_trading_day(n.date()):
        return False
    t = n.time()
    return MORNING[0] <= t <= MORNING[1] or AFTERNOON[0] <= t <= AFTERNOON[1]


def estimable(fund_type: str) -> bool:
    return not any(k in fund_type for k in SKIP_KEYWORDS)


def _stock_symbol(code: str) -> str:
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def stock_quotes(codes: list[str]) -> dict[str, float]:
    """新浪批量行情，返回 {股票代码: 当日涨跌幅%}。缓存 30s。"""
    if not codes:
        return {}
    key = "stk:" + ",".join(sorted(codes))

    def fetch():
        out = {}
        # 新浪单次 URL 有长度限制，分批 60 个
        for i in range(0, len(codes), 60):
            batch = codes[i:i + 60]
            r = SESSION.get("https://hq.sinajs.cn/list=" + ",".join(_stock_symbol(c) for c in batch),
                            headers={"Referer": "https://finance.sina.com.cn"}, timeout=10)
            r.encoding = "gbk"
            for code, line in zip(batch, r.text.strip().splitlines()):
                parts = line.split('"')
                if len(parts) < 2 or not parts[1]:
                    continue
                f = parts[1].split(",")
                if len(f) < 4:
                    continue
                try:
                    prev, cur = float(f[2]), float(f[3])
                except ValueError:
                    continue
                if prev > 0 and cur > 0:          # cur=0 表示停牌，跳过（视为不动）
                    out[code] = (cur / prev - 1) * 100
        return out
    return _cached(key, 30, fetch)


def fund_holdings(code: str) -> tuple[list[tuple[str, float]], str]:
    """基金最新季报重仓股 [(股票代码, 占净值比%)] 与季度标签。缓存 1 天。"""
    def fetch():
        df = ak.fund_portfolio_hold_em(symbol=code, date=str(now_cn().year))
        if df.empty:
            return [], ""
        quarter = df["季度"].iloc[0]              # 已按季度倒序，第一行即最新
        sub = df[df["季度"] == quarter]
        return [(r["股票代码"], float(r["占净值比例"])) for _, r in sub.iterrows()], quarter
    return _cached(f"hold:{code}", 86400, fetch)


def estimate_by_holdings(code: str) -> dict | None:
    """重仓股加权估算当日涨跌幅。"""
    try:
        holds, quarter = fund_holdings(code)
    except Exception:
        return None
    if not holds:
        return None
    quotes = stock_quotes([c for c, _ in holds])
    if not quotes:
        return None
    weighted = sum(w * quotes[c] for c, w in holds if c in quotes)
    coverage = sum(w for c, w in holds if c in quotes)
    if coverage <= 0:
        return None
    return {
        "growth": round(weighted / 100, 2),   # 占比是百分数，除以 100 得加权涨跌幅
        "coverage": round(coverage, 1),
        "source": "重仓股加权",
        "quarter": quarter,
    }


def estimate_by_proxy(proxy_code: str) -> dict | None:
    """代理标的（指数/场内 ETF）实时涨跌幅，精度最高。"""
    from app import data
    q = data.etf_quotes([proxy_code]).get(proxy_code)
    if not q or not q["prev_close"]:
        return None
    return {
        "growth": round((q["price"] / q["prev_close"] - 1) * 100, 2),
        "coverage": 100.0,
        "source": f"跟踪 {q['name']}",
        "quarter": "",
    }


def estimate(code: str, fund_type: str, proxy_code: str | None) -> dict | None:
    if not estimable(fund_type):
        return None
    if proxy_code:
        est = estimate_by_proxy(proxy_code)
        if est:
            return est
    return estimate_by_holdings(code)
