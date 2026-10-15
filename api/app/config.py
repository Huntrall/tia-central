"""Configuração lida das variáveis de ambiente (definidas no Dokploy ou no .env)."""
import os


def _int(name, default):
    return int(os.environ.get(name, default))


def _float(name, default):
    return float(os.environ.get(name, default))


def _bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "sim")


def _list(name, default):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://tia:tia@db:5432/tia")
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")
GRAFANA_RO_PASSWORD = os.environ.get("GRAFANA_RO_PASSWORD", "")
TIMEZONE = os.environ.get("TZ", "America/Sao_Paulo")
GRAFANA_PUBLIC_URL = os.environ.get("GRAFANA_PUBLIC_URL", "").rstrip("/")

# --- GLPI (API REST legada: .../apirest.php) ---
GLPI_URL = os.environ.get("GLPI_URL", "").rstrip("/")
GLPI_APP_TOKEN = os.environ.get("GLPI_APP_TOKEN", "")
GLPI_USER_TOKEN = os.environ.get("GLPI_USER_TOKEN", "")
GLPI_ENTITY_ID = os.environ.get("GLPI_ENTITY_ID", "")
GLPI_CATEGORY_ID = os.environ.get("GLPI_CATEGORY_ID", "")
GLPI_ASSIGN_GROUP_ID = os.environ.get("GLPI_ASSIGN_GROUP_ID", "")
GLPI_AUTO_SOLVE = _bool("GLPI_AUTO_SOLVE", True)
GLPI_VERIFY_TLS = _bool("GLPI_VERIFY_TLS", True)

# --- Regras de alerta ---
ALERT_INTERVAL_SECONDS = _int("ALERT_INTERVAL_SECONDS", 60)
ALERT_CPU_PERCENT = _float("ALERT_CPU_PERCENT", 90)
ALERT_MEMORY_PERCENT = _float("ALERT_MEMORY_PERCENT", 95)
ALERT_SUSTAINED_MINUTES = _int("ALERT_SUSTAINED_MINUTES", 10)
ALERT_NET_MBPS = _float("ALERT_NET_MBPS", 50)
ALERT_NET_MINUTES = _int("ALERT_NET_MINUTES", 15)
ALERT_NET_DAILY_GB = _float("ALERT_NET_DAILY_GB", 20)
# Margem para fechar o alerta (evita abrir/fechar chamado sem parar perto do limite)
ALERT_HYSTERESIS_PERCENT = _float("ALERT_HYSTERESIS_PERCENT", 10)
# Um alerta que normalizou e voltou dentro desta janela reabre o MESMO chamado
ALERT_REOPEN_MINUTES = _int("ALERT_REOPEN_MINUTES", 120)
# Checks do health check do agente que NÃO geram chamado. CPU e memória do health check
# são uma foto instantânea; os alertas deles usam a média sustentada dos heartbeats.
HEALTH_ALERT_IGNORE = _list("HEALTH_ALERT_IGNORE", "cpu,memory,collector,battery")

ALERT_OFFLINE_ENABLED = _bool("ALERT_OFFLINE_ENABLED", False)
ALERT_OFFLINE_MINUTES = _int("ALERT_OFFLINE_MINUTES", 15)
BUSINESS_HOURS = os.environ.get("BUSINESS_HOURS", "08:00-18:00")
BUSINESS_DAYS = [int(d) for d in _list("BUSINESS_DAYS", "1,2,3,4,5")]  # 1 = segunda

# Considera o dado "fresco" para decidir se um alerta pode ser encerrado
METRICS_FRESH_MINUTES = 5
HEALTH_FRESH_MINUTES = 60

RETENTION_DAYS = _int("RETENTION_DAYS", 30)
