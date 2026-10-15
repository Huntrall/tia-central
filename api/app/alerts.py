"""
Motor de alertas: avalia as regras a cada minuto e sincroniza os chamados no GLPI.

Cada regra devolve, por máquina, um de três estados:
  ACTIVE  -> condição crítica presente (abre ou mantém o alerta)
  CLEAR   -> dados recentes mostram que normalizou (encerra o alerta)
  UNKNOWN -> sem dados recentes (máquina desligada/offline): não abre nem encerra nada
"""
import html
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config
from .db import pool
from .glpi import glpi

log = logging.getLogger("tia.alerts")

ACTIVE, CLEAR, UNKNOWN = "active", "clear", "unknown"
URGENCY = {"crit": 4, "warn": 3}  # GLPI: 4 = Alta, 3 = Média


@dataclass
class Condition:
    hostname: str
    rule: str
    state: str
    severity: str = "warn"
    title: str = ""
    details: str = ""


# --- AVALIAÇÃO DAS REGRAS ---

METRICS_QUERY = """
SELECT hostname,
       max(ts) AS last_ts,
       avg(cpu)          FILTER (WHERE ts > now() - make_interval(mins => %(sus)s)) AS cpu_avg,
       avg(mem_percent)  FILTER (WHERE ts > now() - make_interval(mins => %(sus)s)) AS mem_avg,
       count(*)          FILTER (WHERE ts > now() - make_interval(mins => %(sus)s)) AS sus_n,
       (sum(rx_bytes + tx_bytes) FILTER (WHERE ts > now() - make_interval(mins => %(net)s)))::float8 AS net_bytes,
       count(*)          FILTER (WHERE ts > now() - make_interval(mins => %(net)s)) AS net_n,
       (sum(rx_bytes + tx_bytes) FILTER (WHERE ts >= date_trunc('day', now())))::float8 AS today_bytes
FROM metrics
WHERE ts > now() - interval '1 day'
GROUP BY hostname
"""

HEALTH_QUERY = """
SELECT DISTINCT ON (hostname) hostname, ts, checks
FROM health_reports
ORDER BY hostname, ts DESC
"""


def _threshold(value_limit, is_open, percent_points=True):
    """Com o alerta aberto, só encerra abaixo do limite menos a margem (histerese)."""
    if not is_open:
        return value_limit
    if percent_points:
        return value_limit - config.ALERT_HYSTERESIS_PERCENT
    return value_limit * (1 - config.ALERT_HYSTERESIS_PERCENT / 100)


def _ignored_check(check_id):
    return any(check_id == item or check_id.startswith(item + "_") for item in config.HEALTH_ALERT_IGNORE)


def evaluate_metrics(conn, now, open_keys):
    conds = []
    rows = conn.execute(METRICS_QUERY, {"sus": config.ALERT_SUSTAINED_MINUTES, "net": config.ALERT_NET_MINUTES}).fetchall()
    fresh_after = now - timedelta(minutes=config.METRICS_FRESH_MINUTES)
    min_sus = max(1, int(config.ALERT_SUSTAINED_MINUTES * 0.8))
    min_net = max(1, int(config.ALERT_NET_MINUTES * 0.8))

    for r in rows:
        host = r["hostname"]
        fresh = r["last_ts"] > fresh_after

        # CPU e memória: média sustentada, não uma leitura isolada
        for rule, value, limit, label in (
            ("cpu", r["cpu_avg"], config.ALERT_CPU_PERCENT, "CPU"),
            ("memory", r["mem_avg"], config.ALERT_MEMORY_PERCENT, "Memória RAM"),
        ):
            if not fresh or r["sus_n"] < min_sus or value is None:
                conds.append(Condition(host, rule, UNKNOWN))
                continue
            limit_now = _threshold(limit, (host, rule) in open_keys)
            state = ACTIVE if value >= limit_now else CLEAR
            conds.append(Condition(
                host, rule, state, "warn",
                f"{label} acima de {limit:.0f}% há {config.ALERT_SUSTAINED_MINUTES} min",
                f"Média de {value:.0f}% nos últimos {config.ALERT_SUSTAINED_MINUTES} minutos "
                f"(limite {limit:.0f}%)."))

        # Taxa de rede sustentada
        if fresh and r["net_n"] >= min_net:
            mbps = (r["net_bytes"] or 0) * 8 / (r["net_n"] * 60) / 1e6
            limit_now = _threshold(config.ALERT_NET_MBPS, (host, "net_rate") in open_keys, percent_points=False)
            conds.append(Condition(
                host, "net_rate", ACTIVE if mbps >= limit_now else CLEAR, "warn",
                f"Consumo de rede alto: {mbps:.0f} Mbps",
                f"Média de {mbps:.1f} Mbps (download + upload) nos últimos {config.ALERT_NET_MINUTES} minutos "
                f"(limite {config.ALERT_NET_MBPS:.0f} Mbps)."))
        else:
            conds.append(Condition(host, "net_rate", UNKNOWN))

        # Volume diário (zera à meia-noite; encerra no dia seguinte quando a máquina voltar)
        today_gb = (r["today_bytes"] or 0) / 1e9
        if fresh:
            conds.append(Condition(
                host, "net_daily", ACTIVE if today_gb >= config.ALERT_NET_DAILY_GB else CLEAR, "warn",
                f"Consumo diário de rede: {today_gb:.1f} GB",
                f"A máquina já trafegou {today_gb:.1f} GB hoje (limite {config.ALERT_NET_DAILY_GB:.0f} GB)."))
        else:
            conds.append(Condition(host, "net_daily", UNKNOWN))
    return conds


def evaluate_health(conn, now, open_keys):
    conds = []
    fresh_after = now - timedelta(minutes=config.HEALTH_FRESH_MINUTES)
    for r in conn.execute(HEALTH_QUERY).fetchall():
        host = r["hostname"]
        host_open = {rule for (h, rule) in open_keys if h == host and rule.startswith("health:")}
        if r["ts"] <= fresh_after:
            conds.extend(Condition(host, rule, UNKNOWN) for rule in host_open)
            continue

        active = {}
        for check in r["checks"]:
            check_id = str(check.get("id", ""))
            if check.get("status") != "crit" or not check_id or _ignored_check(check_id):
                continue
            rule = f"health:{check_id}"
            hint = check.get("hint") or ""
            active[rule] = Condition(
                host, rule, ACTIVE, "crit",
                f"{check.get('label')}: {check.get('value')}",
                f"{check.get('label')}: {check.get('value')}. {hint}".strip())
        conds.extend(active.values())
        # Alertas de saúde abertos cujo check não está mais crítico no relatório recente
        conds.extend(Condition(host, rule, CLEAR) for rule in host_open - active.keys())
    return conds


def _in_business_hours(moment):
    start, end = config.BUSINESS_HOURS.split("-")
    return (moment.isoweekday() in config.BUSINESS_DAYS
            and start <= moment.strftime("%H:%M") < end)


def evaluate_offline(conn, now, open_keys):
    if not config.ALERT_OFFLINE_ENABLED:
        return []
    tz = ZoneInfo(config.TIMEZONE)
    local_now = now.astimezone(tz)
    conds = []
    limit = now - timedelta(minutes=config.ALERT_OFFLINE_MINUTES)
    for r in conn.execute("SELECT hostname, last_seen FROM devices").fetchall():
        last_local = r["last_seen"].astimezone(tz)
        if r["last_seen"] > limit:
            conds.append(Condition(r["hostname"], "offline", CLEAR))
        # Só alerta se a máquina trabalhou hoje em horário comercial e sumiu durante o expediente.
        # Máquinas desligadas à noite ou que nem foram ligadas hoje não geram chamado.
        elif (_in_business_hours(local_now) and _in_business_hours(last_local)
              and last_local.date() == local_now.date()):
            minutes = int((now - r["last_seen"]).total_seconds() // 60)
            conds.append(Condition(
                r["hostname"], "offline", ACTIVE, "warn",
                f"Sem comunicação há {minutes} min",
                f"A máquina parou de enviar dados às {last_local:%H:%M}, durante o expediente. "
                "Pode estar desligada, sem rede ou com o agente T.I.A. parado."))
        else:
            conds.append(Condition(r["hostname"], "offline", UNKNOWN))
    return conds


# --- SINCRONIZAÇÃO COM O GLPI ---

def _ticket_content(cond, device):
    lines = [
        f"<p>{html.escape(cond.details)}</p>",
        "<ul>",
        f"<li><b>Máquina:</b> {html.escape(cond.hostname)}</li>",
        f"<li><b>Usuário logado:</b> {html.escape(device.get('username') or '—')}</li>",
        f"<li><b>IP:</b> {html.escape(device.get('ip') or '—')}</li>",
        f"<li><b>Sistema:</b> {html.escape(device.get('os') or '—')}</li>",
        "</ul>",
    ]
    if config.GRAFANA_PUBLIC_URL:
        url = f"{config.GRAFANA_PUBLIC_URL}/d/tia-device?var-host={cond.hostname}"
        lines.append(f'<p><a href="{html.escape(url)}">Ver histórico da máquina no painel</a></p>')
    lines.append("<p><i>Chamado aberto automaticamente pelo T.I.A. Central.</i></p>")
    return "\n".join(lines)


def _glpi_call(conn, alert_id, action):
    """Executa uma ação no GLPI e registra o erro no alerta (para nova tentativa no próximo ciclo)."""
    if not glpi.enabled:
        return None
    try:
        result = action()
        conn.execute("UPDATE alerts SET glpi_error = NULL WHERE id = %s", (alert_id,))
        return result
    except Exception as e:  # GLPIError, rede, timeout, resposta inesperada
        log.error("GLPI (alerta %s): %s", alert_id, e)
        conn.execute("UPDATE alerts SET glpi_error = %s WHERE id = %s", (str(e)[:500], alert_id))
        return None


def _open_ticket(conn, alert_id, cond, device):
    ticket_id = _glpi_call(conn, alert_id, lambda: glpi.create_ticket(
        f"[T.I.A.] {cond.hostname} — {cond.title}", _ticket_content(cond, device),
        URGENCY[cond.severity], cond.hostname))
    if ticket_id:
        conn.execute("UPDATE alerts SET glpi_ticket_id = %s WHERE id = %s", (ticket_id, alert_id))
        log.info("Chamado GLPI #%s aberto: %s / %s", ticket_id, cond.hostname, cond.rule)


def reconcile(conn, conditions, now):
    devices = {d["hostname"]: d for d in conn.execute("SELECT * FROM devices").fetchall()}
    open_alerts = {(a["hostname"], a["rule"]): a
                   for a in conn.execute("SELECT * FROM alerts WHERE status = 'open'").fetchall()}

    for cond in conditions:
        key = (cond.hostname, cond.rule)
        alert = open_alerts.get(key)
        device = devices.get(cond.hostname, {})

        if cond.state == ACTIVE and alert is None:
            recent = conn.execute(
                """SELECT * FROM alerts WHERE hostname = %s AND rule = %s AND status = 'resolved'
                   AND resolved_at > %s ORDER BY resolved_at DESC LIMIT 1""",
                (cond.hostname, cond.rule, now - timedelta(minutes=config.ALERT_REOPEN_MINUTES))).fetchone()
            if recent:
                # Voltou pouco depois de normalizar: reabre o mesmo alerta/chamado em vez de criar outro
                conn.execute(
                    """UPDATE alerts SET status = 'open', resolved_at = NULL, updated_at = now(),
                       title = %s, details = %s, severity = %s WHERE id = %s""",
                    (cond.title, cond.details, cond.severity, recent["id"]))
                log.info("Alerta reaberto: %s / %s", cond.hostname, cond.rule)
                if recent["glpi_ticket_id"]:
                    _glpi_call(conn, recent["id"], lambda: glpi.reopen_ticket(
                        recent["glpi_ticket_id"], f"<p>O problema voltou: {html.escape(cond.details)}</p>"))
                else:
                    _open_ticket(conn, recent["id"], cond, device)
            else:
                alert_id = conn.execute(
                    """INSERT INTO alerts (hostname, rule, severity, title, details)
                       VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                    (cond.hostname, cond.rule, cond.severity, cond.title, cond.details)).fetchone()["id"]
                log.info("Alerta aberto: %s / %s — %s", cond.hostname, cond.rule, cond.title)
                _open_ticket(conn, alert_id, cond, device)

        elif cond.state == ACTIVE and alert is not None:
            conn.execute("UPDATE alerts SET details = %s, title = %s, updated_at = now() WHERE id = %s",
                         (cond.details, cond.title, alert["id"]))
            if alert["glpi_ticket_id"] is None:
                _open_ticket(conn, alert["id"], cond, device)  # nova tentativa após falha no GLPI

        elif cond.state == CLEAR and alert is not None:
            conn.execute("UPDATE alerts SET status = 'resolved', resolved_at = now(), updated_at = now() WHERE id = %s",
                         (alert["id"],))
            log.info("Alerta normalizado: %s / %s", cond.hostname, cond.rule)
            ticket_id = alert["glpi_ticket_id"]
            if ticket_id:
                message = f"<p>Indicador normalizado automaticamente em {now.astimezone(ZoneInfo(config.TIMEZONE)):%d/%m/%Y %H:%M}.</p>"
                if config.GLPI_AUTO_SOLVE:
                    _glpi_call(conn, alert["id"], lambda: glpi.solve_ticket(ticket_id, message))
                else:
                    _glpi_call(conn, alert["id"], lambda: glpi.add_followup(ticket_id, message))


def run_cycle():
    now = datetime.now().astimezone()
    with pool.connection() as conn:
        open_keys = {(a["hostname"], a["rule"])
                     for a in conn.execute("SELECT hostname, rule FROM alerts WHERE status = 'open'").fetchall()}
        conditions = (evaluate_metrics(conn, now, open_keys)
                      + evaluate_health(conn, now, open_keys)
                      + evaluate_offline(conn, now, open_keys))
        reconcile(conn, conditions, now)


def purge_old_data():
    with pool.connection() as conn:
        days = config.RETENTION_DAYS
        m = conn.execute("DELETE FROM metrics WHERE ts < now() - make_interval(days => %s)", (days,)).rowcount
        h = conn.execute("DELETE FROM health_reports WHERE ts < now() - make_interval(days => %s)", (days,)).rowcount
        if m or h:
            log.info("Retenção: %s métricas e %s relatórios de saúde antigos removidos.", m, h)


def start_background_loop(stop_event: threading.Event):
    def loop():
        last_purge = 0.0
        while not stop_event.is_set():
            try:
                run_cycle()
                if time.monotonic() - last_purge > 3600:
                    purge_old_data()
                    last_purge = time.monotonic()
            except Exception:
                log.exception("Erro no ciclo de alertas")
            stop_event.wait(config.ALERT_INTERVAL_SECONDS)

    thread = threading.Thread(target=loop, name="tia-alerts", daemon=True)
    thread.start()
    return thread
