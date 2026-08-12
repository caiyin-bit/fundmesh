"""持仓账本：交易流水 CRUD + 持仓/收益计算（全部由流水 × 净值派生）。"""

import json
import re
from datetime import date as Date, timedelta

from app import data, estimate
from app.data import SESSION
from app.db import pool

UA = {"User-Agent": "Mozilla/5.0", "Referer": "http://fund.eastmoney.com/"}


# ---------- 净值同步（pingzhongdata：一次请求全量历史） ----------

def sync_nav(code: str) -> str:
    """拉取全量净值/万份收益入库，返回资产小类 'fund' | 'money'。"""
    r = SESSION.get(
        f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
        headers=UA, timeout=15,
    )
    r.raise_for_status()
    text = r.text

    def block(name):
        m = re.search(rf"{name}\s*=\s*(\[.*?\]);", text)
        return json.loads(m.group(1)) if m else []

    nav = block("Data_netWorthTrend")
    if nav:
        rows = [(code, _ts2date(p["x"]), p["y"], p.get("equityReturn"), None) for p in nav]
        kind = "fund"
    else:
        rows = [(code, _ts2date(ts), 1.0, None, v) for ts, v in block("Data_millionCopiesIncome")]
        kind = "money"

    with pool.connection() as conn:
        conn.cursor().executemany(
            "INSERT INTO nav_history(code,date,nav,growth,income) VALUES(%s,%s,%s,%s,%s) "
            "ON CONFLICT (code,date) DO UPDATE SET "
            "nav = EXCLUDED.nav, growth = EXCLUDED.growth, income = EXCLUDED.income",
            rows,
        )
    return kind


def sync_etf_history(code: str, start: Date) -> None:
    """场内 ETF 日线收盘价入库（与基金净值共用 nav_history，语义都是"每份价值"）。"""
    rows = data.etf_history(code, start.isoformat(), Date.today().isoformat())
    if not rows:
        return
    with pool.connection() as conn:
        conn.cursor().executemany(
            "INSERT INTO nav_history(code,date,nav,growth,income) VALUES(%s,%s,%s,NULL,NULL) "
            "ON CONFLICT (code,date) DO UPDATE SET nav = EXCLUDED.nav",
            [(code, d, v) for d, v in rows],
        )


def _ts2date(ms: int) -> Date:
    # 东财时间戳是北京时间零点，按 UTC 转会早一天，需 +8h
    return Date(1970, 1, 1) + timedelta(milliseconds=ms, hours=8)


def nav_on(code: str, day: Date) -> float | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT nav FROM nav_history WHERE code=%s AND date=%s", (code, day)
        ).fetchone()
    return row["nav"] if row else None


# ---------- 资产档案 ----------

def ensure_asset(code: str, asset: str) -> dict:
    with pool.connection() as conn:
        row = conn.execute("SELECT * FROM assets WHERE code=%s", (code,)).fetchone()
    if row:
        return row

    if asset == "etf":
        q = data.etf_quotes([code]).get(code)
        if not q:
            raise ValueError(f"未找到场内代码 {code} 的行情")
        rec = {"code": code, "name": q["name"], "type": "场内ETF", "asset": "etf"}
    else:
        flist = data.fund_list_df()
        hit = flist[flist["基金代码"] == code]
        if hit.empty:
            raise ValueError(f"未找到基金 {code}")
        kind = sync_nav(code)  # 顺带判断是否货基
        rec = {"code": code, "name": hit.iloc[0]["基金简称"],
               "type": hit.iloc[0]["基金类型"], "asset": kind}

    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO assets(code,name,type,asset) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT (code) DO NOTHING",
            (rec["code"], rec["name"], rec["type"], rec["asset"]),
        )
    return rec


# ---------- 流水 ----------

def add_transaction(code: str, asset: str, type_: str, day: str, amount: float,
                    shares: float | None, price: float | None, fee: float, note: str) -> dict:
    day = Date.fromisoformat(day)
    rec = ensure_asset(code, asset)
    kind = rec["asset"]
    if type_ in ("buy", "sell") and not shares:
        if kind == "money":
            shares = amount  # 货基 1 元/份
            price = 1.0
        elif kind == "fund":
            price = price or nav_on(code, day)
            if price is None:
                sync_nav(code)
                price = nav_on(code, day)
            if price is None:
                raise ValueError(f"{day} 无 {code} 净值（非交易日或净值未公布），请改日期或手填份额")
            shares = round(amount / price, 2)
        else:  # etf 必须给成交价或份额
            if not price:
                raise ValueError("场内 ETF 请填写成交价或份额")
            shares = round(amount / price, 2)

    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO transactions(code,type,date,amount,shares,price,fee,note) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (code, type_, day, amount, shares or 0, price, fee, note),
        ).fetchone()
    return row


def list_transactions(code: str | None = None) -> list[dict]:
    with pool.connection() as conn:
        if code:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE code=%s ORDER BY date DESC, id DESC", (code,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM transactions ORDER BY date DESC, id DESC").fetchall()
    return rows


def delete_transaction(tid: int) -> bool:
    with pool.connection() as conn:
        cur = conn.execute("DELETE FROM transactions WHERE id=%s", (tid,))
    return cur.rowcount > 0


# ---------- 持仓计算 ----------

def _money_accrued(code: str, flows: list[dict]) -> tuple[float, float, Date | None]:
    """货基累计收益、最新一日收益、收益数据日期。按流水逐日累加万份收益。"""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT date, income FROM nav_history WHERE code=%s AND income IS NOT NULL ORDER BY date",
            (code,),
        ).fetchall()
    events = sorted(flows, key=lambda f: f["date"])
    accrued, latest_daily, latest_date = 0.0, 0.0, None
    shares, i = 0.0, 0
    for r in rows:
        while i < len(events) and events[i]["date"] <= r["date"]:
            f = events[i]
            shares += f["shares"] if f["type"] == "buy" else -f["shares"] if f["type"] == "sell" else 0
            i += 1
        if shares > 0 and r["income"]:
            daily = shares / 10000 * r["income"]
            accrued += daily
            latest_daily, latest_date = daily, r["date"]
    return round(accrued, 2), round(latest_daily, 2), latest_date


def _cash_flows(flows: list[dict]) -> list[tuple[Date, float]]:
    """现金流：买入为负（流出），卖出/分红为正（流入）。"""
    out = []
    for f in flows:
        if f["type"] == "buy":
            out.append((f["date"], -(f["amount"] + f["fee"])))
        elif f["type"] == "sell":
            out.append((f["date"], f["amount"] - f["fee"]))
        else:
            out.append((f["date"], f["amount"]))
    return out


def xirr(cash_flows: list[tuple[Date, float]]) -> float | None:
    """资金加权年化收益率。牛顿法求解，不收敛则二分兜底。"""
    if len(cash_flows) < 2:
        return None
    t0 = min(d for d, _ in cash_flows)
    years = [((d - t0).days / 365.0, v) for d, v in cash_flows]
    if not (any(v < 0 for _, v in years) and any(v > 0 for _, v in years)):
        return None

    def npv(rate):
        return sum(v / (1 + rate) ** t for t, v in years)

    rate = 0.1
    for _ in range(50):
        f = npv(rate)
        df = sum(-t * v / (1 + rate) ** (t + 1) for t, v in years)
        if abs(df) < 1e-12:
            break
        step = f / df
        rate -= step
        if rate <= -0.9999:
            rate = -0.99
        if abs(step) < 1e-8:
            return round(rate * 100, 2)

    lo, hi = -0.9999, 100.0                     # 牛顿法失败时二分
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2 * 100, 2)


def curve() -> list[dict]:
    """组合逐日市值与累计投入。由流水 × 历史净值全量回溯，不依赖任何快照。"""
    flows = sorted(list_transactions(), key=lambda f: f["date"])
    if not flows:
        return []
    start = flows[0]["date"]
    codes = {f["code"] for f in flows}

    with pool.connection() as conn:
        assets = {r["code"]: r for r in conn.execute("SELECT * FROM assets").fetchall()}
        # 场内 ETF 的历史序列按需增量补齐：首次从建仓日拉全量，之后只补最新一段
        for c in codes:
            if assets.get(c, {}).get("asset") != "etf":
                continue
            latest = conn.execute(
                "SELECT MAX(date) AS d FROM nav_history WHERE code=%s", (c,)
            ).fetchone()["d"]
            if latest is None:
                sync_etf_history(c, start)
            elif latest < Date.today():
                sync_etf_history(c, latest)
        rows = conn.execute(
            "SELECT code, date, nav, income FROM nav_history "
            "WHERE code = ANY(%s) AND date >= %s ORDER BY date",
            (list(codes), start),
        ).fetchall()
    if not rows:
        return []

    series: dict[str, dict[Date, dict]] = {}
    for r in rows:
        series.setdefault(r["code"], {})[r["date"]] = r
    dates = sorted({r["date"] for r in rows})

    shares = dict.fromkeys(codes, 0.0)
    accrued = dict.fromkeys(codes, 0.0)          # 货基累计收益
    last_nav = dict.fromkeys(codes, 0.0)         # 停牌/无数据时前值填充
    cost, i, points = 0.0, 0, []

    for d in dates:
        while i < len(flows) and flows[i]["date"] <= d:
            f = flows[i]
            if f["type"] == "buy":
                shares[f["code"]] += f["shares"]
                cost += f["amount"] + f["fee"]
            elif f["type"] == "sell":
                shares[f["code"]] -= f["shares"]
                cost -= f["amount"] - f["fee"]
            else:
                cost -= f["amount"]
            i += 1

        value = 0.0
        for c in codes:
            row = series.get(c, {}).get(d)
            if assets.get(c, {}).get("asset") == "money":
                if row and row["income"]:
                    accrued[c] += shares[c] / 10000 * row["income"]
                value += shares[c] + accrued[c]
            else:
                if row and row["nav"]:
                    last_nav[c] = row["nav"]
                value += shares[c] * last_nav[c]
        points.append({"date": d, "value": round(value, 2), "cost": round(cost, 2)})

    return points


def _save_official_navs(navs: dict[str, dict]) -> None:
    """把最新官方净值写回库。曲线依赖 nav_history，不回写会随时间变陈旧；
    同时让当日 est_growth 与 growth 并存，才能算出估值偏差。"""
    rows = [(c, n["date"], n["nav"], n["growth"]) for c, n in navs.items() if n.get("nav")]
    if not rows:
        return
    with pool.connection() as conn:
        conn.cursor().executemany(
            "INSERT INTO nav_history(code,date,nav,growth) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT (code,date) DO UPDATE SET nav = EXCLUDED.nav, growth = EXCLUDED.growth",
            rows,
        )


def _save_estimates(day: Date, estimates: dict[str, dict]) -> None:
    """留存当日估算值。官方净值到账后与 growth 并存，可回看估值偏差。"""
    with pool.connection() as conn:
        conn.cursor().executemany(
            "INSERT INTO nav_history(code,date,est_growth) VALUES(%s,%s,%s) "
            "ON CONFLICT (code,date) DO UPDATE SET est_growth = EXCLUDED.est_growth",
            [(c, day, e["growth"]) for c, e in estimates.items()],
        )


def _estimate_errors(codes: list[str]) -> dict[str, dict]:
    """最近一次"估算 vs 官方"的偏差，单位百分点。"""
    if not codes:
        return {}
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (code) code, date, growth - est_growth AS err "
            "FROM nav_history WHERE code = ANY(%s) "
            "AND est_growth IS NOT NULL AND growth IS NOT NULL "
            "ORDER BY code, date DESC",
            (codes,),
        ).fetchall()
    return {r["code"]: {"date": r["date"], "error": round(r["err"], 2)} for r in rows}


def holdings() -> dict:
    flows = list_transactions()
    if not flows:
        return {"summary": {"value": 0, "day_pnl": 0, "total_pnl": 0, "cost": 0}, "holdings": []}
    by_code: dict[str, list[dict]] = {}
    for f in flows:
        by_code.setdefault(f["code"], []).append(f)

    with pool.connection() as conn:
        assets = {r["code"]: r for r in conn.execute("SELECT * FROM assets").fetchall()}

    fund_codes = [c for c in by_code if assets.get(c, {}).get("asset") in ("fund", "money")]
    etf_codes = [c for c in by_code if assets.get(c, {}).get("asset") == "etf"]
    navs = data.batch_latest_nav(fund_codes) if fund_codes else {}
    quotes = data.etf_quotes(etf_codes) if etf_codes else {}

    if navs:
        _save_official_navs(navs)

    # 官方净值未出的基金才需要估值；同时取回历史估值偏差用于展示可信度
    today = Date.today()
    estimates = {}
    for c in fund_codes:
        a = assets.get(c, {})
        n = navs.get(c)
        if a.get("asset") == "fund" and n and n["date"] < today:
            est = estimate.estimate(c, a.get("type", ""), a.get("proxy_code"))
            if est:
                estimates[c] = est
    if estimates:
        _save_estimates(today, estimates)
    est_errors = _estimate_errors(list(fund_codes))

    out = []
    for code, fl in by_code.items():
        a = assets.get(code, {})
        kind = a.get("asset", "fund")
        shares = sum(f["shares"] if f["type"] == "buy" else -f["shares"] if f["type"] == "sell" else 0 for f in fl)
        cash_out = sum(f["amount"] + f["fee"] for f in fl if f["type"] == "buy")
        cash_in = sum(f["amount"] - f["fee"] for f in fl if f["type"] == "sell")
        cash_in += sum(f["amount"] for f in fl if f["type"] == "dividend")
        net_cost = cash_out - cash_in

        h = {"code": code, "name": a.get("name", code), "type": a.get("type", ""),
             "asset": kind, "shares": round(shares, 2), "net_cost": round(net_cost, 2)}

        if kind == "money":
            # 简化模型：市值 = 剩余本金份额 + 历史累计万份收益（未做复投计份）
            accrued, daily, ddate = _money_accrued(code, fl)
            h.update(value=round(shares + accrued, 2), day_pnl=daily, nav=1.0, nav_date=ddate)
        elif kind == "etf":
            q = quotes.get(code)
            price, prev = (q["price"], q["prev_close"]) if q else (None, None)
            h.update(value=round(shares * price, 2) if price else None,
                     day_pnl=round(shares * (price - prev), 2) if price else None,
                     nav=price, nav_date=q["date"] if q else "")
        else:
            n = navs.get(code)
            if not n:
                h.update(value=None, day_pnl=None, nav=None, nav_date="")
            elif n["date"] < Date.today() and (est := estimates.get(code)):
                # 官方净值尚未公布，用盘中估算值叠加在最近一期净值上
                est_nav = n["nav"] * (1 + est["growth"] / 100)
                h.update(value=round(shares * est_nav, 2),
                         day_pnl=round(shares * (est_nav - n["nav"]), 2),
                         nav=round(est_nav, 4), nav_date=Date.today(), day_growth=est["growth"],
                         estimated=True, est_coverage=est["coverage"], est_source=est["source"])
            else:
                nav, chg = n["nav"], n["growth"]
                prev = nav / (1 + chg / 100) if chg is not None else nav
                h.update(value=round(shares * nav, 2),
                         day_pnl=round(shares * (nav - prev), 2),
                         nav=nav, nav_date=n["date"], day_growth=chg)
            if code in est_errors:
                h["est_error"] = est_errors[code]

        if h.get("value") is not None:
            h["total_pnl"] = round(h["value"] + cash_in - cash_out, 2)
            h["pnl_rate"] = round(h["total_pnl"] / cash_out * 100, 2) if cash_out else None
        out.append(h)

    total_value = sum(h["value"] or 0 for h in out)
    for h in out:
        h["weight"] = round((h["value"] or 0) / total_value * 100, 1) if total_value else 0
    summary = {
        "value": round(total_value, 2),
        "day_pnl": round(sum(h["day_pnl"] or 0 for h in out), 2),
        "total_pnl": round(sum(h.get("total_pnl") or 0 for h in out), 2),
        "cost": round(sum(h["net_cost"] for h in out), 2),
    }
    summary["pnl_rate"] = round(summary["total_pnl"] / summary["cost"] * 100, 2) if summary["cost"] else None
    # 年化：把当前市值当作今天全部赎回的一笔流入
    summary["xirr"] = xirr(_cash_flows(flows) + [(Date.today(), total_value)])
    out.sort(key=lambda h: -(h["value"] or 0))
    return {"summary": summary, "holdings": out}
