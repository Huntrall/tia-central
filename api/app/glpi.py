"""Cliente da API REST legada do GLPI (apirest.php), disponível no GLPI 9.5, 10 e 11."""
import logging
import threading

import httpx

from . import config

log = logging.getLogger("tia.glpi")

STATUS_PROCESSING = 2  # "Em atendimento (atribuído)"
TICKET_TYPE_INCIDENT = 1


class GLPIError(Exception):
    pass


class GLPIClient:
    def __init__(self):
        self._session_token = None
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=20, verify=config.GLPI_VERIFY_TLS)

    @property
    def enabled(self):
        return bool(config.GLPI_URL and config.GLPI_APP_TOKEN and config.GLPI_USER_TOKEN)

    # --- Sessão ---

    def _init_session(self):
        resp = self._http.get(
            f"{config.GLPI_URL}/initSession",
            headers={"App-Token": config.GLPI_APP_TOKEN, "Authorization": f"user_token {config.GLPI_USER_TOKEN}"},
        )
        if resp.status_code != 200:
            raise GLPIError(f"initSession falhou (HTTP {resp.status_code}): {resp.text[:300]}")
        self._session_token = resp.json()["session_token"]

    def _request(self, method, path, **kwargs):
        with self._lock:
            for attempt in range(2):
                if not self._session_token:
                    self._init_session()
                headers = {"App-Token": config.GLPI_APP_TOKEN, "Session-Token": self._session_token}
                resp = self._http.request(method, f"{config.GLPI_URL}/{path}", headers=headers, **kwargs)
                # Sessão expirada: renova uma vez e tenta de novo
                if resp.status_code == 401 and attempt == 0:
                    self._session_token = None
                    continue
                if resp.status_code >= 400:
                    raise GLPIError(f"{method} {path} falhou (HTTP {resp.status_code}): {resp.text[:300]}")
                return resp.json() if resp.content else None

    # --- Operações usadas pelo motor de alertas ---

    def find_computer_id(self, hostname):
        """Procura o computador inventariado (GLPI-Agent) pelo nome, para vincular ao chamado."""
        params = {
            "criteria[0][field]": "1",            # 1 = nome
            "criteria[0][searchtype]": "equals",
            "criteria[0][value]": hostname,
            "forcedisplay[0]": "2",               # 2 = ID
            "range": "0-0",
        }
        try:
            result = self._request("GET", "search/Computer", params=params)
        except GLPIError as e:
            log.warning("Não foi possível buscar o computador %s no GLPI: %s", hostname, e)
            return None
        rows = (result or {}).get("data") or []
        return int(rows[0]["2"]) if rows else None

    def create_ticket(self, title, content, urgency, hostname):
        ticket = {
            "name": title,
            "content": content,
            "type": TICKET_TYPE_INCIDENT,
            "urgency": urgency,
            "impact": urgency,
        }
        if config.GLPI_ENTITY_ID:
            ticket["entities_id"] = int(config.GLPI_ENTITY_ID)
        if config.GLPI_CATEGORY_ID:
            ticket["itilcategories_id"] = int(config.GLPI_CATEGORY_ID)
        if config.GLPI_ASSIGN_GROUP_ID:
            ticket["_groups_id_assign"] = int(config.GLPI_ASSIGN_GROUP_ID)

        result = self._request("POST", "Ticket", json={"input": ticket})
        ticket_id = int(result["id"])

        computer_id = self.find_computer_id(hostname)
        if computer_id:
            try:
                self._request("POST", "Item_Ticket", json={"input": {
                    "itemtype": "Computer", "items_id": computer_id, "tickets_id": ticket_id}})
            except GLPIError as e:
                log.warning("Chamado %s criado, mas falhou ao vincular o computador: %s", ticket_id, e)
        return ticket_id

    def add_followup(self, ticket_id, content):
        self._request("POST", "ITILFollowup", json={"input": {
            "itemtype": "Ticket", "items_id": ticket_id, "content": content}})

    def solve_ticket(self, ticket_id, content):
        self._request("POST", "ITILSolution", json={"input": {
            "itemtype": "Ticket", "items_id": ticket_id, "content": content}})

    def reopen_ticket(self, ticket_id, content):
        self.add_followup(ticket_id, content)
        self._request("PUT", f"Ticket/{ticket_id}", json={"input": {"id": ticket_id, "status": STATUS_PROCESSING}})


glpi = GLPIClient()
