"""Pool de conexões e inicialização do esquema."""
import logging
from pathlib import Path

from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

log = logging.getLogger("tia.db")

pool = ConnectionPool(
    config.DATABASE_URL,
    min_size=1,
    max_size=10,
    open=False,
    kwargs={"row_factory": dict_row, "autocommit": True, "options": f"-c timezone={config.TIMEZONE}"},
)


def init_schema():
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    with pool.connection() as conn:
        conn.execute(schema)
        dbname = conn.info.dbname
        # Fuso padrão do banco: vale para a API e para as consultas do Grafana ("hoje" = dia local)
        conn.execute(sql.SQL("ALTER DATABASE {} SET timezone TO {}").format(
            sql.Identifier(dbname), sql.Literal(config.TIMEZONE)))
        _ensure_grafana_reader(conn, dbname)
    log.info("Esquema do banco verificado.")


def _ensure_grafana_reader(conn, dbname):
    """Cria/atualiza o usuário somente-leitura usado pelo Grafana."""
    if not config.GRAFANA_RO_PASSWORD:
        log.warning("GRAFANA_RO_PASSWORD não definido: usuário de leitura do Grafana não foi criado.")
        return
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro'").fetchone()
    verb = "ALTER" if exists else "CREATE"
    conn.execute(sql.SQL(verb + " ROLE grafana_ro LOGIN PASSWORD {}").format(
        sql.Literal(config.GRAFANA_RO_PASSWORD)))
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO grafana_ro").format(sql.Identifier(dbname)))
    conn.execute("GRANT USAGE ON SCHEMA public TO grafana_ro")
    conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro")
    conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro")
