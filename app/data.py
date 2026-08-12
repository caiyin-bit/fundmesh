"""akshare 数据封装 + 内存 TTL 缓存。"""

import threading
import time
from datetime import date

import akshare as ak
import numpy as np
import pandas as pd
import requests

# 国内源直连最稳：不读环境变量/系统代理（本机代理对东财接口时好时坏）；
# 东财部分接口封 python-requests 默认 UA，统一用浏览器 UA
SESSION = requests.Session()
SESSION.trust_env = False
SESSION.headers["User-Agent"] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()

KEY_INDICES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000300", "沪深300"),
    ("sh000905", "中证500"),
    ("sh000688", "科创50"),
]

RANK_CATEGORIES = ["全部", "股票型", "混合型", "债券型", "指数型", "QDII", "FOF"]

RANK_NUMERIC_COLS = [
    "单位净值", "累计净值", "日增长率", "近1周", "近1月", "近3月",
    "近6月", "近1年", "近2年", "近3年", "今年来", "成立来",
]


def _cached(key: str, ttl: float, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def indices() -> list[dict]:
    """六大核心指数实时行情，缓存 60s。"""
    def fetch():
        df = ak.stock_zh_index_spot_sina()
        codes = [c for c, _ in KEY_INDICES]
        df = df[df["代码"].isin(codes)].set_index("代码")
        out = []
        for code, name in KEY_INDICES:
            if code not in df.index:
                continue
            row = df.loc[code]
            out.append({
                "code": code,
                "name": name,
                "price": round(float(row["最新价"]), 2),
                "change_pct": round(float(row["涨跌幅"]), 2),
                "change_amt": round(float(row["涨跌额"]), 2),
            })
        return out
    return _cached("indices", 60, fetch)


def rank_df(category: str) -> pd.DataFrame:
    """开放式基金排行（全市场约 2 万只），数值列已转 float，缓存 10 分钟。"""
    def fetch():
        df = ak.fund_open_fund_rank_em(symbol=category)
        for col in RANK_NUMERIC_COLS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    return _cached(f"rank:{category}", 600, fetch)


def fund_list_df() -> pd.DataFrame:
    """全部基金代码/名称/类型/拼音缩写，缓存 1 天。"""
    return _cached("fund_list", 86400, ak.fund_name_em)


def search(q: str, limit: int = 20) -> list[dict]:
    df = fund_list_df()
    q = q.strip()
    qu = q.upper()
    mask = (
        df["基金代码"].str.startswith(q)
        | df["基金简称"].str.contains(q, regex=False)
        | df["拼音缩写"].str.contains(qu, regex=False)
    )
    hits = df[mask].head(limit)
    return [
        {"code": r["基金代码"], "name": r["基金简称"], "type": r["基金类型"]}
        for _, r in hits.iterrows()
    ]


def batch_latest_nav(codes: list[str]) -> dict[str, dict]:
    """天天基金 App 接口：一次查多只基金最新净值。缓存 60s。"""
    key = "batch_nav:" + ",".join(sorted(codes))

    def fetch():
        r = SESSION.get(
            "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo",
            params={"Fcodes": ",".join(codes), "pageIndex": 1, "pageSize": len(codes),
                    "plat": "Android", "appType": "ttjj", "product": "EFund",
                    "Version": 1, "deviceid": "fundmesh"},
            timeout=10,
        )
        out = {}
        for d in r.json().get("Datas") or []:
            try:
                out[d["FCODE"]] = {
                    "nav": float(d["NAV"]),
                    "growth": float(d["NAVCHGRT"]) if d.get("NAVCHGRT") not in (None, "--") else None,
                    "date": date.fromisoformat(d["PDATE"]),   # 与库内 DATE 列可直接比较
                }
            except (TypeError, ValueError):
                continue
        return out
    return _cached(key, 60, fetch)


def _etf_symbol(code: str) -> str:
    return ("sh" if code.startswith(("5", "6")) else "sz") + code


def etf_quotes(codes: list[str]) -> dict[str, dict]:
    """新浪行情：场内 ETF/LOF 实时价。缓存 30s。"""
    key = "etf:" + ",".join(sorted(codes))

    def fetch():
        symbols = ",".join(_etf_symbol(c) for c in codes)
        r = SESSION.get(f"https://hq.sinajs.cn/list={symbols}",
                         headers={"Referer": "https://finance.sina.com.cn"}, timeout=10)
        r.encoding = "gbk"
        out = {}
        for code, line in zip(codes, r.text.strip().splitlines()):
            m = line.split('"')
            if len(m) < 2 or not m[1]:
                continue
            f = m[1].split(",")
            if len(f) < 32 or float(f[3]) == 0:
                continue
            out[code] = {"name": f[0], "price": float(f[3]), "prev_close": float(f[2]),
                         "open": float(f[1]), "date": f[30], "time": f[31]}
        return out
    return _cached(key, 30, fetch)


def etf_history(code: str, start: str, end: str) -> list[tuple[str, float]]:
    """腾讯行情：场内 ETF 日线收盘价。返回 [(日期, 收盘价)]。

    单次上限 640 条，故按窗口向前分段拉取。只有前复权(qfq)可用——它以最新价为锚，
    终点与实时价一致，历史点按分红回调（总回报视角）。
    """
    sym = _etf_symbol(code)
    out: dict[str, float] = {}
    cursor = end
    while cursor > start:
        r = SESSION.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                        params={"param": f"{sym},day,{start},{cursor},640,qfq"}, timeout=20)
        d = r.json().get("data", {}).get(sym) or {}
        rows = d.get("qfqday") or d.get("day") or []
        if not rows:
            break
        for x in rows:
            out[x[0]] = float(x[2])          # [日期, 开, 收, 高, 低, 量]
        earliest = rows[0][0]
        if earliest <= start or len(rows) < 640:
            break
        cursor = earliest                     # 继续向前取上一段
    return sorted(out.items())


def fund_detail(code: str) -> dict | None:
    flist = fund_list_df()
    row = flist[flist["基金代码"] == code]
    if row.empty:
        return None
    info = {"code": code, "name": row.iloc[0]["基金简称"], "type": row.iloc[0]["基金类型"]}

    def fetch_nav():
        df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        return [
            {"date": str(r["净值日期"]), "nav": r["单位净值"], "growth": r["日增长率"]}
            for _, r in df.iterrows()
        ]
    info["nav_history"] = _cached(f"nav:{code}", 3600, fetch_nav)

    # 区间涨幅：从全部排行缓存里取该基金一行（排行未加载/未收录则为空）
    perf = {}
    with _lock:
        hit = _cache.get("rank:全部")
    if hit is not None:
        df = hit[1]
        r = df[df["基金代码"] == code]
        if not r.empty:
            r = r.iloc[0].replace({np.nan: None})
            perf = {k: r[k] for k in RANK_NUMERIC_COLS if k in r.index}
    info["performance"] = perf
    return info
