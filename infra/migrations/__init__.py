"""轻量级自研迁移运行器（infra.migrations）。

单一入口 run_migrations()：用 schema_version 表追踪已应用版本，按版本号升序
应用未应用的迁移。既有库(已建表)用 to_regclass 探针「盖章」基线而不重跑 DDL，
全新库则按 scripts/init_db.py 的确切顺序跑基线 DDL 后盖章。
"""

from swarm.infra.migrations.runner import run_migrations

__all__ = ["run_migrations"]
