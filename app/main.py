"""FundMesh — 国内基金数据大盘。启动: uv run uvicorn app.main:app"""

from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import data, db, estimate, portfolio


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield
    db.pool.close()


app = FastAPI(title="FundMesh", lifespan=lifespan)

SORTABLE = set(data.RANK_NUMERIC_COLS)


@app.get("/api/indices")
def api_indices():
    return data.indices()


@app.get("/api/rank")
def api_rank(
    category: str = Query("全部"),
    sort: str = Query("日增长率"),
    order: str = Query("desc"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
):
    if category not in data.RANK_CATEGORIES:
        raise HTTPException(400, f"category 必须是 {data.RANK_CATEGORIES}")
    if sort not in SORTABLE:
        raise HTTPException(400, f"sort 必须是 {sorted(SORTABLE)}")
    df = data.rank_df(category)
    df = df.sort_values(sort, ascending=(order == "asc"), na_position="last")
    total = len(df)
    rows = df.iloc[(page - 1) * size : page * size].replace({np.nan: None})
    cols = ["基金代码", "基金简称", "日期", "单位净值", "日增长率",
            "近1周", "近1月", "近3月", "近6月", "近1年", "近3年", "今年来"]
    cols = [c for c in cols if c in rows.columns]
    return {"total": total, "page": page, "size": size,
            "rows": rows[cols].to_dict("records")}


@app.get("/api/search")
def api_search(q: str = Query(..., min_length=1)):
    return data.search(q)


@app.get("/api/fund/{code}")
def api_fund(code: str):
    info = data.fund_detail(code)
    if info is None:
        raise HTTPException(404, f"未找到基金 {code}")
    return info


class TxIn(BaseModel):
    code: str
    asset: str = "fund"          # 'fund' | 'etf'（货基自动识别）
    type: str                    # 'buy' | 'sell' | 'dividend'
    date: str                    # YYYY-MM-DD
    amount: float
    shares: float | None = None
    price: float | None = None
    fee: float = 0
    note: str = ""


@app.get("/api/portfolio")
def api_portfolio():
    return portfolio.holdings()


@app.get("/api/market-status")
def api_market_status():
    return {"trading": estimate.is_trading_now(),
            "time": estimate.now_cn().strftime("%Y-%m-%d %H:%M")}


@app.get("/api/portfolio/curve")
def api_portfolio_curve():
    return portfolio.curve()


@app.get("/api/transactions")
def api_transactions(code: str | None = None):
    return portfolio.list_transactions(code)


@app.post("/api/transactions")
def api_add_transaction(tx: TxIn):
    if tx.type not in ("buy", "sell", "dividend"):
        raise HTTPException(400, "type 必须是 buy/sell/dividend")
    try:
        return portfolio.add_transaction(tx.code, tx.asset, tx.type, tx.date,
                                         tx.amount, tx.shares, tx.price, tx.fee, tx.note)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/transactions/{tid}")
def api_delete_transaction(tid: int):
    if not portfolio.delete_transaction(tid):
        raise HTTPException(404, "流水不存在")
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "static", html=True))
