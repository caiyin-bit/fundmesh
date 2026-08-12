"""PostgreSQL + TimescaleDB 持久层。

分两类负载：
  - assets / transactions：普通表，账本，量小、要事务
  - nav_history 等行情表：hypertable，按时间分片 + 列式压缩，供回测扫描
"""

import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DSN = os.environ.get(
    "FUNDMESH_DSN",
    "postgresql://fundmesh:fundmesh@localhost:5433/fundmesh",
)

pool = ConnectionPool(DSN, min_size=1, max_size=10, kwargs={"row_factory": dict_row}, open=False)

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  code       TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  type       TEXT NOT NULL,
  asset      TEXT NOT NULL DEFAULT 'fund',
  proxy_code TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
  id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code   TEXT NOT NULL,
  type   TEXT NOT NULL CHECK (type IN ('buy','sell','dividend')),
  date   DATE NOT NULL,
  amount DOUBLE PRECISION NOT NULL,
  shares DOUBLE PRECISION NOT NULL DEFAULT 0,
  price  DOUBLE PRECISION,
  fee    DOUBLE PRECISION NOT NULL DEFAULT 0,
  note   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS nav_history (
  code   TEXT NOT NULL,
  date   DATE NOT NULL,
  nav    DOUBLE PRECISION,
  growth DOUBLE PRECISION,
  income DOUBLE PRECISION,
  PRIMARY KEY (code, date)
);
"""

# 行情表转 hypertable + 列式压缩，供回测扫描。TimescaleDB 不可用时跳过，
# 表结构完全一致，装上扩展后再执行即可（见 docker-compose.yml 注释）。
HYPERTABLE = """
SELECT create_hypertable('nav_history', 'date',
                         chunk_time_interval => INTERVAL '1 year',
                         migrate_data => TRUE, if_not_exists => TRUE);

ALTER TABLE nav_history SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'code',
  timescaledb.compress_orderby   = 'date DESC'
);

SELECT add_compression_policy('nav_history', INTERVAL '1 year', if_not_exists => TRUE);
"""


def timescale_available(conn) -> bool:
    cur = conn.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
    return cur.fetchone() is not None


def init_db() -> None:
    pool.open()
    with pool.connection() as conn:
        conn.execute(SCHEMA)
        if not timescale_available(conn):
            return
        conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        cur = conn.execute(
            "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = 'nav_history'"
        )
        if not cur.fetchone():
            conn.execute(HYPERTABLE)
