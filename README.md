# T.I.A. Central

Monitoramento centralizado dos computadores que rodam o agente **T.I.A.**: recebe métricas
(CPU, memória, consumo de rede) e health checks, mostra tudo no Grafana e **abre chamados no
GLPI** quando algum indicador fica crítico — e soluciona o chamado quando normaliza.

```
 PCs com T.I.A. ──HTTP──▶ API (FastAPI) ──▶ PostgreSQL ◀── Grafana (painéis)
  (a cada 1 min)              │
                              └── motor de alertas (a cada 1 min) ──▶ GLPI (chamados)
```

## Componentes

| Serviço   | Porta | Função |
|-----------|-------|--------|
| `api`     | 8000  | Recebe os dados dos agentes e roda o motor de alertas |
| `db`      | —     | PostgreSQL 16 (não exposto) |
| `grafana` | 3000  | Painéis "Visão geral da frota" e "Detalhe da máquina" |

## Deploy no Dokploy

1. Suba este repositório para o GitHub.
2. No Dokploy: **Create Service → Compose**, aponte para o repositório, branch `main`,
   arquivo `docker-compose.yml`.
3. Em **Environment**, cole o conteúdo do `.env.example` e preencha os valores
   (gere senhas/tokens com `openssl rand -hex 24`).
4. **Deploy**. A API cria as tabelas e o usuário de leitura do Grafana sozinha.
5. Acesse `http://IP-DO-SERVIDOR:3000` (usuário `admin`). O painel da frota abre como página inicial.

Opcional: crie registros DNS no AD (ex.: `tia-central.empresa.local`, `tia-monitor.empresa.local`)
e configure-os em **Domains** no Dokploy (serviço `api` porta 8000, serviço `grafana` porta 3000).

Teste rápido da API: `curl http://IP-DO-SERVIDOR:8000/healthz` → `{"status":"ok"}`.

## Configurar o GLPI

1. **Configurar → Geral → API**: ative *API REST* e *login com token externo*.
2. Na mesma tela, adicione um **cliente da API** (restrinja ao IP do servidor Dokploy) e copie o
   **App-Token** → `GLPI_APP_TOKEN`.
3. Crie um usuário técnico (ex.: `tia-central`) com perfil que possa criar chamados e soluções.
   Em **Administração → Usuários → (usuário) → Chaves de acesso remoto**, gere o
   **token da API** → `GLPI_USER_TOKEN`.
4. `GLPI_URL` é o endereço do GLPI terminado em `/apirest.php`.
5. Opcional: `GLPI_CATEGORY_ID` (categoria ITIL), `GLPI_ASSIGN_GROUP_ID` (grupo técnico que recebe),
   `GLPI_ENTITY_ID`.

Os chamados são vinculados automaticamente ao **computador inventariado pelo GLPI-Agent** quando o
nome bate. Falhas de comunicação com o GLPI ficam registradas no alerta (painel "Falhas de envio ao
GLPI") e a API tenta de novo a cada minuto.

## Configurar os agentes (GPO)

No `.env` distribuído junto com a T.I.A., adicione:

```
CENTRAL_URL=http://IP-DO-SERVIDOR:8000
CENTRAL_TOKEN=<mesmo valor de AGENT_TOKEN>
```

Sem essas variáveis o agente funciona só localmente. Com elas, envia um heartbeat por minuto e um
health check a cada 15 minutos. Se a central estiver inacessível, guarda até 2 dias de dados em
`%LOCALAPPDATA%\TIA\outbox.json` e envia quando a conexão voltar.

## Regras de alerta

| Regra | Dispara quando | Encerra quando |
|-------|----------------|----------------|
| CPU | média ≥ `ALERT_CPU_PERCENT` por `ALERT_SUSTAINED_MINUTES` | média < limite − 10 pontos |
| Memória | média ≥ `ALERT_MEMORY_PERCENT` pelo mesmo período | média < limite − 10 pontos |
| Rede (taxa) | média ≥ `ALERT_NET_MBPS` por `ALERT_NET_MINUTES` | média < 90% do limite |
| Rede (diária) | tráfego do dia ≥ `ALERT_NET_DAILY_GB` | no dia seguinte |
| Health check | qualquer item **crítico** (disco cheio, falha SMART, antivírus desligado, 14+ dias sem reiniciar...) | o próximo health check vem sem o item crítico |
| Offline (opcional) | a máquina trabalhou hoje no expediente e parou de enviar há `ALERT_OFFLINE_MINUTES` | voltou a enviar |

Comportamentos importantes:

- **Sem dados, nada muda**: uma máquina desligada não tem o chamado solucionado por engano.
- **Um alerta por máquina e regra**: enquanto continua crítico, o chamado não é duplicado.
- **Volta em menos de 2 h** (`ALERT_REOPEN_MINUTES`): reabre o *mesmo* chamado com um
  acompanhamento, em vez de abrir outro.

## Testar com dados simulados

```bash
python scripts/simulate_agents.py --url http://IP-DO-SERVIDOR:8000 --token SEU_AGENT_TOKEN --hosts 20
```

Cria 20 máquinas `SIM-PC-*` (3 delas com problemas) e envia 1 heartbeat/min. **Ele abre chamados de
verdade se o GLPI estiver configurado.** Para limpar depois:

```sql
DELETE FROM devices WHERE hostname LIKE 'SIM-%';
```

## Volume e retenção

Com 200 máquinas: ~288 mil linhas de métricas por dia (~9 milhões em 30 dias), tranquilo para o
PostgreSQL. Dados mais antigos que `RETENTION_DAYS` são apagados automaticamente a cada hora.
Configure um backup periódico do volume `tia-db` (por exemplo, `pg_dump` agendado no servidor).
