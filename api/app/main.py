"""T.I.A. Central — recebe métricas e health checks dos agentes e gera alertas no GLPI."""
import hmac
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from . import alerts, config
from .db import init_schema, pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tia.api")

# Amostras com relógio muito adiantado são descartadas (protege a lógica de "dado recente")
MAX_CLOCK_SKEW = timedelta(minutes=10)


# --- Modelos ---

class DeviceInfo(BaseModel):
    username: Optional[str] = Field(None, max_length=128)
    ip: Optional[str] = Field(None, max_length=64)
    os: Optional[str] = Field(None, max_length=128)
    agent_version: Optional[str] = Field(None, max_length=32)


class MetricSample(BaseModel):
    ts: datetime
    cpu: Optional[float] = Field(None, ge=0, le=100)
    mem_percent: Optional[float] = Field(None, ge=0, le=100)
    uptime_hours: Optional[float] = Field(None, ge=0)
    rx_bytes: int = Field(0, ge=0)
    tx_bytes: int = Field(0, ge=0)


class MetricsPayload(BaseModel):
    hostname: str = Field(..., min_length=1, max_length=64)
    info: DeviceInfo = DeviceInfo()
    samples: list[MetricSample] = Field(..., max_length=2000)


class HealthCheck(BaseModel):
    id: str = Field(..., max_length=64)
    label: str = Field(..., max_length=200)
    value: str = Field("", max_length=300)
    status: str = Field(..., pattern="^(ok|warn|crit)$")
    hint: str = Field("", max_length=500)


class HealthPayload(BaseModel):
    hostname: str = Field(..., min_length=1, max_length=64)
    info: DeviceInfo = DeviceInfo()
    ts: datetime
    overall: str = Field(..., pattern="^(ok|warn|crit)$")
    checks: list[HealthCheck] = Field(..., max_length=100)
    top_processes: list[dict] = Field(default_factory=list, max_length=20)


# --- Autenticação dos agentes ---

def require_agent_token(x_agent_token: str = Header("")):
    if not config.AGENT_TOKEN or not hmac.compare_digest(x_agent_token, config.AGENT_TOKEN):
        raise HTTPException(status_code=401, detail="Token do agente inválido.")


# --- Aplicação ---

_stop = threading.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.AGENT_TOKEN:
        log.warning("AGENT_TOKEN não definido: todos os envios dos agentes serão recusados.")
    pool.open(wait=True, timeout=60)
    init_schema()
    alerts.start_background_loop(_stop)
    log.info("GLPI %s.", "configurado" if alerts.glpi.enabled else "NÃO configurado (alertas só ficam no banco)")
    yield
    _stop.set()
    pool.close()


app = FastAPI(title="T.I.A. Central", lifespan=lifespan)


def _upsert_device(conn, hostname, info: DeviceInfo, seen_at):
    conn.execute(
        """INSERT INTO devices (hostname, username, ip, os, agent_version, last_seen)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (hostname) DO UPDATE SET
               username      = COALESCE(EXCLUDED.username, devices.username),
               ip            = COALESCE(EXCLUDED.ip, devices.ip),
               os            = COALESCE(EXCLUDED.os, devices.os),
               agent_version = COALESCE(EXCLUDED.agent_version, devices.agent_version),
               last_seen     = GREATEST(devices.last_seen, EXCLUDED.last_seen)""",
        (hostname, info.username, info.ip, info.os, info.agent_version, seen_at))


@app.get("/healthz")
def healthz():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/api/v1/metrics", dependencies=[Depends(require_agent_token)])
def ingest_metrics(payload: MetricsPayload):
    hostname = payload.hostname.upper()
    now = datetime.now(timezone.utc)
    samples = [s for s in payload.samples if s.ts <= now + MAX_CLOCK_SKEW]
    if not samples:
        return {"accepted": 0}
    with pool.connection() as conn, conn.transaction():
        # last_seen = momento da amostra mais recente (um envio atrasado da fila local
        # não deve fazer uma máquina offline parecer online)
        _upsert_device(conn, hostname, payload.info, min(max(s.ts for s in samples), now))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO metrics (hostname, ts, cpu, mem_percent, uptime_hours, rx_bytes, tx_bytes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (hostname, ts) DO NOTHING""",
                [(hostname, s.ts, s.cpu, s.mem_percent, s.uptime_hours, s.rx_bytes, s.tx_bytes) for s in samples])
    return {"accepted": len(samples)}


@app.post("/api/v1/health", dependencies=[Depends(require_agent_token)])
def ingest_health(payload: HealthPayload):
    hostname = payload.hostname.upper()
    now = datetime.now(timezone.utc)
    ts = min(payload.ts, now)
    with pool.connection() as conn, conn.transaction():
        _upsert_device(conn, hostname, payload.info, ts)
        conn.execute(
            "INSERT INTO health_reports (hostname, ts, overall, checks, top_processes) VALUES (%s, %s, %s, %s, %s)",
            (hostname, ts, payload.overall, Jsonb([c.model_dump() for c in payload.checks]),
             Jsonb(payload.top_processes)))
    return {"accepted": 1}
