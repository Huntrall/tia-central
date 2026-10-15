"""
Simula agentes T.I.A. enviando dados para a central (apenas biblioteca padrão).

Uso:
    python scripts/simulate_agents.py --url http://localhost:8000 --token SEU_TOKEN --hosts 20

Envia as últimas N horas de métricas de uma vez (como a fila local de um agente faria)
e depois continua enviando um heartbeat por minuto. Algumas máquinas simulam problemas:
  SIM-PC-001  CPU travada em ~97%
  SIM-PC-002  disco com falha SMART (health check crítico)
  SIM-PC-003  baixando ~80 Mbps sem parar
Use os prefixos com cuidado: os dados simulados vão para o mesmo banco dos reais
(apague depois com:  DELETE FROM devices WHERE hostname LIKE 'SIM-%';).
"""
import argparse
import json
import random
import time
import urllib.request
from datetime import datetime, timedelta, timezone


def post(url, token, path, payload):
    req = urllib.request.Request(
        url.rstrip("/") + path, data=json.dumps(payload, default=str).encode(),
        headers={"Content-Type": "application/json", "X-Agent-Token": token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def sample(host_index, ts):
    cpu = random.uniform(5, 35)
    mem = random.uniform(45, 75)
    rx = random.randint(50_000, 4_000_000)
    tx = random.randint(20_000, 800_000)
    if host_index == 1:
        cpu = random.uniform(93, 99)
    if host_index == 3:
        rx = int(80e6 / 8 * 60 * random.uniform(0.9, 1.1))  # ~80 Mbps no minuto
    return {"ts": ts.isoformat(), "cpu": round(cpu, 1), "mem_percent": round(mem, 1),
            "uptime_hours": 30.5, "rx_bytes": rx, "tx_bytes": tx}


def health(host_index, hostname, ts):
    disk_ok = host_index != 2
    checks = [
        {"id": "cpu", "label": "Processador (CPU)", "value": "20% em uso", "status": "ok", "hint": ""},
        {"id": "memory", "label": "Memória RAM", "value": "62% em uso", "status": "ok", "hint": ""},
        {"id": "uptime", "label": "Tempo ligado sem reiniciar", "value": "1.3 dias", "status": "ok", "hint": ""},
        {"id": "disk_C:", "label": "Espaço em disco C:", "value": "80 GB livres de 237 GB (34%)", "status": "ok", "hint": ""},
        {"id": "disk_health", "label": "Saúde do disco (SSD 256GB)", "value": "OK" if disk_ok else "Pred Fail",
         "status": "ok" if disk_ok else "crit",
         "hint": "" if disk_ok else "O disco reportou falha (SMART). Faça backup e abra um chamado imediatamente."},
        {"id": "antivirus", "label": "Antivírus (Defender)", "value": "Ativo, assinaturas de 0 dia(s) atrás", "status": "ok", "hint": ""},
    ]
    overall = "crit" if not disk_ok else "ok"
    return {"hostname": hostname, "ts": ts.isoformat(), "overall": overall, "checks": checks,
            "top_processes": [{"name": "chrome", "mem_mb": 1200}, {"name": "Teams", "mem_mb": 800}],
            "info": {"username": f"usuario{host_index}", "ip": f"10.0.{host_index // 50}.{host_index % 250 + 10}",
                     "os": "Microsoft Windows 11 Pro", "agent_version": "sim"}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--token", required=True)
    parser.add_argument("--hosts", type=int, default=20)
    parser.add_argument("--backfill-hours", type=float, default=2)
    parser.add_argument("--once", action="store_true", help="envia o histórico e sai")
    args = parser.parse_args()

    hosts = [(i, f"SIM-PC-{i:03d}") for i in range(1, args.hosts + 1)]
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    minutes = int(args.backfill_hours * 60)

    for i, name in hosts:
        info = health(i, name, now)["info"]
        samples = [sample(i, now - timedelta(minutes=m)) for m in range(minutes, -1, -1)]
        post(args.url, args.token, "/api/v1/metrics", {"hostname": name, "info": info, "samples": samples})
        post(args.url, args.token, "/api/v1/health", health(i, name, now))
    print(f"{len(hosts)} máquinas simuladas com {minutes} min de histórico.")

    while not args.once:
        time.sleep(60)
        ts = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        for i, name in hosts:
            post(args.url, args.token, "/api/v1/metrics", {"hostname": name, "samples": [sample(i, ts)]})
        print(f"{ts:%H:%M} heartbeat enviado para {len(hosts)} máquinas.")


if __name__ == "__main__":
    main()
