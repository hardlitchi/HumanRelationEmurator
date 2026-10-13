"""実 PostgreSQL に使い捨ての DB を作ってテストする。

HRE_TEST_ADMIN_URL（既定: postgresql://postgres@localhost:5432/postgres）に CREATE DATABASE できること。
"""

import os
import subprocess
import sys
import uuid
from dataclasses import dataclass

import psycopg
import pytest

from hre_server import service

ADMIN_URL = os.environ.get("HRE_TEST_ADMIN_URL", "postgresql://postgres@localhost:5432/postgres")
DB_NAME = "hre_pytest"


@pytest.fixture(scope="session")
def database_url():
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {DB_NAME}")
    url = psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=DB_NAME)
    os.environ["DATABASE_URL"] = url
    # 管理 CLI 経由で初期化（CLI 自体のテストも兼ねる）
    subprocess.run([sys.executable, "-m", "hre_server.admin", "init-db"], check=True, env=os.environ)
    yield url


@pytest.fixture
def conn(database_url):
    with psycopg.connect(database_url, autocommit=True) as c:
        yield c


@dataclass
class World:
    """管理者・AITuber A/B・グループ共有のワークスペースと、それぞれのエージェント。"""
    a: str
    b: str
    group: str
    admin: service.Actor
    owner_human: service.Actor
    agent_a: service.Actor
    agent_b: service.Actor
    tokens: dict


@pytest.fixture
def world(conn) -> World:
    sfx = uuid.uuid4().hex[:8]
    a, b, group = f"aituber-a-{sfx}", f"aituber-b-{sfx}", f"group-{sfx}"
    service.create_workspace(conn, a, "AITuber A", "persona")
    service.create_workspace(conn, b, "AITuber B", "persona")
    service.create_workspace(conn, group, "グループ共有", "shared")
    service.share(conn, group, a)
    service.share(conn, group, b)

    tokens = {}
    ids = {}
    for name, kind, admin in [("admin", "human", True), ("owner", "human", False),
                              ("agent-a", "agent", False), ("agent-b", "agent", False)]:
        ids[name], tokens[name] = service.create_actor(conn, f"{name}-{sfx}", kind, admin)
    service.grant(conn, f"owner-{sfx}", a, ["read", "write", "sensitive_read"])
    service.grant(conn, f"agent-a-{sfx}", a, ["read", "write"])
    service.grant(conn, f"agent-b-{sfx}", b, ["read", "write"])

    def load(n):
        return service.load_actor(conn, ids[n])

    return World(a, b, group, load("admin"), load("owner"), load("agent-a"), load("agent-b"), tokens)
