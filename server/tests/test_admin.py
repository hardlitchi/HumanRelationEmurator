"""hre-admin の初回セットアップ（bootstrap）。他のテストの管理者と混ざらないよう専用の DB で確かめる。"""

import os
import subprocess
import sys

import psycopg
import pytest

from hre_server import service

ADMIN_URL = os.environ.get("HRE_TEST_ADMIN_URL", "postgresql://postgres@localhost:5432/postgres")

DB_NAME = "hre_pytest_bootstrap"


@pytest.fixture
def fresh_url():
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {DB_NAME}")
    url = psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=DB_NAME)
    _admin(url, "init-db")
    yield url
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")


def _admin(url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "hre_server.admin", *args], capture_output=True, text=True,
                          env={**os.environ, "DATABASE_URL": url})


def test_bootstrap_creates_first_admin_once(fresh_url):
    with psycopg.connect(fresh_url, autocommit=True) as conn:
        assert not service.has_admin(conn)

    r = _admin(fresh_url, "bootstrap", "ta")
    assert r.returncode == 0, r.stderr
    token = r.stdout.strip().splitlines()[-1]
    assert token.startswith("hre_")

    with psycopg.connect(fresh_url, autocommit=True) as conn:
        assert service.has_admin(conn)
        actor = service.authenticate(conn, token)
        assert actor is not None and actor.kind == "human" and actor.is_admin
        scopes = conn.execute("""SELECT g.scopes FROM hre.workspace_grants g
                                 JOIN hre.workspaces w ON w.id = g.workspace_id
                                 WHERE g.actor_id = %s AND w.key = 'owner'""", (actor.id,)).fetchone()[0]
        assert set(scopes) == {"read", "write", "sensitive_read"}

    # 2回目は管理者を増やさない
    r = _admin(fresh_url, "bootstrap", "someone-else")
    assert r.returncode != 0
    assert "rotate-token" in r.stderr
    with psycopg.connect(fresh_url, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM hre.actors").fetchone()[0] == 1
