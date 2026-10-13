import atexit
import os
from functools import cache

from psycopg_pool import ConnectionPool


@cache
def pool() -> ConnectionPool:
    """DATABASE_URL への接続プール。各操作は conn.transaction() で明示的にトランザクションを張る。"""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL を設定してください")
    p = ConnectionPool(url, kwargs={"autocommit": True}, min_size=1, max_size=10, open=True)
    atexit.register(p.close)   # 閉じないとワーカースレッドの終了待ちでプロセスの終了が遅れる
    return p
