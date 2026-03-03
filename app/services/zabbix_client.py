import httpx
import logging
from app.database import fetch_one

logger = logging.getLogger(__name__)


async def get_zabbix_config() -> dict:
    try:
        row = await fetch_one("SELECT * FROM zabbix_config LIMIT 1")
        return row or {"url": "", "token": "", "refresh": 30}
    except Exception:
        return {"url": "", "token": "", "refresh": 30}


async def call_zabbix(method: str, params: dict = None, cfg_override: dict = None):
    cfg = cfg_override or await get_zabbix_config()
    url = cfg["url"].rstrip("/") + "/api_jsonrpc.php"
    token = cfg.get("token", "")

    headers = {"Content-Type": "application/json"}
    if method != "apiinfo.version":
        headers["Authorization"] = f"Bearer {token}"

    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": 1,
    }

    try:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            data = resp.json()
    except Exception as e:
        logger.error(f"Zabbix call failed: {e}")
        return None

    if "error" in data:
        err = data["error"]
        return {"_zabbix_error": err.get("data") or err.get("message") or "Zabbix error"}

    return data.get("result", [])
