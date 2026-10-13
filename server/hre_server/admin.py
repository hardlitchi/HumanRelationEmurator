"""管理用コマンド。DATABASE_URL に対して実行する。

  python -m hre_server.admin init-db
  python -m hre_server.admin bootstrap                                  # 最初の管理者を作り、ログイン用トークンを表示
  python -m hre_server.admin create-workspace aituber-a "AITuber A" persona
  python -m hre_server.admin create-actor aituber-a-agent agent        # トークンは1度だけ表示
  python -m hre_server.admin grant aituber-a-agent aituber-a read,write
  python -m hre_server.admin share group aituber-a                     # aituber-a から group を読めるようにする
  python -m hre_server.admin add-persona rio "秋霞莉央" --group fuyuakinatsuharu   # 上の 4 つを 1 人分まとめて
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg

from . import service

DB_DIR = Path(__file__).resolve().parents[2] / "db"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="db/schema.sql・seed.sql・oauth.sql を適用する（空の DB に1回だけ）")
    b = sub.add_parser("bootstrap", help="最初の管理者と管理者用ワークスペース owner を作り、ログイン用トークンを表示する")
    b.add_argument("name", nargs="?", default="admin", help="管理者の名前（既定: admin）")
    w = sub.add_parser("create-workspace")
    w.add_argument("key")
    w.add_argument("name")
    w.add_argument("kind", choices=["owner", "persona", "shared"])
    a = sub.add_parser("create-actor")
    a.add_argument("name")
    a.add_argument("kind", choices=["human", "agent", "importer"])
    a.add_argument("--admin", action="store_true")
    r = sub.add_parser("rotate-token")
    r.add_argument("name")
    v = sub.add_parser("revoke")
    v.add_argument("name")
    g = sub.add_parser("grant")
    g.add_argument("actor")
    g.add_argument("workspace")
    g.add_argument("scopes", help="read,write,sensitive_read のカンマ区切り")
    s = sub.add_parser("share")
    s.add_argument("workspace", help="共有される側（例: group）")
    s.add_argument("reader", help="読む側（例: aituber-a）")
    pa = sub.add_parser("add-persona", help="AITuber 1 人分のワークスペース・「自分」・エージェントを作り、トークンを表示する")
    pa.add_argument("key", help="ワークスペースのキー（例: rio）")
    pa.add_argument("name", help="キャラの名前（共有ワークスペースの同じ名前の人と同一人物になる）")
    pa.add_argument("--group", help="読めるようにする共有ワークスペース（例: fuyuakinatsuharu）")
    pa.add_argument("--actor", help="エージェント名（既定: <key>-agent）")
    args = p.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        match args.cmd:
            case "init-db":
                for f in ("schema.sql", "seed.sql", "oauth.sql"):
                    conn.execute((DB_DIR / f).read_text())
                print("schema and seed applied")
            case "bootstrap":
                try:
                    token = service.bootstrap(conn, args.name)
                except service.HreError as e:
                    raise SystemExit(str(e))
                print(f"管理者 {args.name} を作りました。Web UI のログイン画面に次のトークンを入力してください（表示はこの1回だけ）:\n{token}")
            case "create-workspace":
                service.create_workspace(conn, args.key, args.name, args.kind)
                print(f"workspace {args.key} created")
            case "create-actor":
                _, token = service.create_actor(conn, args.name, args.kind, args.admin)
                print(f"actor {args.name} created. token (shown once):\n{token}")
            case "rotate-token":
                print(service.rotate_token(conn, args.name))
            case "revoke":
                service.revoke_actor(conn, args.name)
                print(f"actor {args.name} revoked")
            case "grant":
                service.grant(conn, args.actor, args.workspace, args.scopes.split(","))
                print(f"granted {args.scopes} on {args.workspace} to {args.actor}")
            case "share":
                service.share(conn, args.workspace, args.reader)
                print(f"{args.reader} can now read {args.workspace}")
            case "add-persona":
                try:
                    _, token = service.add_persona(conn, args.key, args.name, args.group, args.actor)
                except service.HreError as e:
                    raise SystemExit(str(e))
                print(f"persona {args.key}（{args.name}）ready. token (shown once):\n{token}")


if __name__ == "__main__":
    main()
