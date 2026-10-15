-- Esquema criado/atualizado automaticamente na inicialização da API.

CREATE TABLE IF NOT EXISTS devices (
    hostname       TEXT PRIMARY KEY,
    username       TEXT,
    ip             TEXT,
    os             TEXT,
    agent_version  TEXT,
    first_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Uma linha por minuto por máquina (heartbeat)
CREATE TABLE IF NOT EXISTS metrics (
    hostname      TEXT NOT NULL REFERENCES devices(hostname) ON DELETE CASCADE,
    ts            TIMESTAMPTZ NOT NULL,
    cpu           REAL,
    mem_percent   REAL,
    uptime_hours  REAL,
    rx_bytes      BIGINT NOT NULL DEFAULT 0,
    tx_bytes      BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (hostname, ts)
);
CREATE INDEX IF NOT EXISTS metrics_ts_idx ON metrics (ts);

CREATE TABLE IF NOT EXISTS health_reports (
    id             BIGSERIAL PRIMARY KEY,
    hostname       TEXT NOT NULL REFERENCES devices(hostname) ON DELETE CASCADE,
    ts             TIMESTAMPTZ NOT NULL,
    overall        TEXT NOT NULL,
    checks         JSONB NOT NULL,
    top_processes  JSONB NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS health_reports_host_ts_idx ON health_reports (hostname, ts DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id              BIGSERIAL PRIMARY KEY,
    hostname        TEXT NOT NULL REFERENCES devices(hostname) ON DELETE CASCADE,
    rule            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    title           TEXT NOT NULL,
    details         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',   -- open | resolved
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    glpi_ticket_id  INTEGER,
    glpi_error      TEXT
);
-- No máximo um alerta aberto por máquina + regra
CREATE UNIQUE INDEX IF NOT EXISTS alerts_one_open_idx ON alerts (hostname, rule) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS alerts_host_rule_idx ON alerts (hostname, rule, resolved_at DESC);

-- Visão mais recente de cada máquina, usada pelos painéis do Grafana
CREATE OR REPLACE VIEW device_overview AS
SELECT
    d.hostname,
    d.username,
    d.ip,
    d.os,
    d.agent_version,
    d.last_seen,
    (d.last_seen > now() - interval '3 minutes') AS online,
    m.cpu,
    m.mem_percent,
    m.uptime_hours,
    h.overall AS health,
    h.ts AS health_ts,
    COALESCE(t.today_bytes, 0) AS today_bytes,
    COALESCE(a.open_alerts, 0) AS open_alerts
FROM devices d
LEFT JOIN LATERAL (
    SELECT cpu, mem_percent, uptime_hours FROM metrics
    WHERE hostname = d.hostname ORDER BY ts DESC LIMIT 1
) m ON true
LEFT JOIN LATERAL (
    SELECT overall, ts FROM health_reports
    WHERE hostname = d.hostname ORDER BY ts DESC LIMIT 1
) h ON true
LEFT JOIN LATERAL (
    SELECT sum(rx_bytes + tx_bytes) AS today_bytes FROM metrics
    WHERE hostname = d.hostname AND ts >= date_trunc('day', now())
) t ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS open_alerts FROM alerts
    WHERE hostname = d.hostname AND status = 'open'
) a ON true;
