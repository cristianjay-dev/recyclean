# dashboard/services/reloadly.py
import time
import requests
from django.conf import settings

# simple in-memory token cache
_token_cache = {"access_token": None, "exp": 0}

class ReloadlyError(Exception):
    pass

def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": getattr(settings, "RELOADLY_ACCEPT_HEADER", "application/com.reloadly.topups-v1+json"),
        "Content-Type": "application/json",
    }

def get_access_token() -> str:
    """Get (and cache) a Reloadly OAuth token."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["exp"] - 60:
        return _token_cache["access_token"]

    url = f"{settings.RELOADLY_AUTH_BASE}/oauth/token"
    payload = {
        "client_id": settings.RELOADLY_CLIENT_ID,
        "client_secret": settings.RELOADLY_CLIENT_SECRET,
        "grant_type": "client_credentials",
        "audience": settings.RELOADLY_AUDIENCE,
    }
    resp = requests.post(url, json=payload, timeout=20)
    if not resp.ok:
        raise ReloadlyError(f"Auth failed: {resp.status_code} {resp.text}")
    data = resp.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["exp"] = time.time() + int(data.get("expires_in", 1800))
    return _token_cache["access_token"]

# -------- Reporting --------

def get_reloadly_balance():
    """GET /accounts/balance -> current Airtime wallet balance."""
    token = get_access_token()
    url = f"{settings.RELOADLY_TOPUPS_BASE}/accounts/balance"
    resp = requests.get(url, headers=_headers(token), timeout=15)
    resp.raise_for_status()
    return resp.json()  # includes balance, currencyCode, updatedAt

def list_reloadly_transactions(page: int = 1, size: int = 20):
    """GET /topups/reports/transactions -> list of top-up transactions (paginated)."""
    token = get_access_token()
    url = f"{settings.RELOADLY_TOPUPS_BASE}/topups/reports/transactions"
    params = {"page": page, "size": size}
    resp = requests.get(url, headers=_headers(token), params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and "content" in data:
        return data["content"]
    return data if isinstance(data, list) else []

# -------- Topups --------

def auto_detect_operator(phone: str, country_code: str = "PH") -> dict:
    """GET operator by phone."""
    token = get_access_token()
    url = f"{settings.RELOADLY_TOPUPS_BASE}/operators/auto-detect/phone/{phone}/countries/{country_code}"
    resp = requests.get(url, headers=_headers(token), timeout=20)
    if not resp.ok:
        raise ReloadlyError(f"Auto-detect failed: {resp.status_code} {resp.text}")
    return resp.json()

def send_topup(phone: str, amount: float, operator_id: int | None = None, country_code: str = "PH") -> dict:
    """POST /topups -> send mobile load."""
    token = get_access_token()
    if operator_id is None:
        op = auto_detect_operator(phone, country_code)
        operator_id = op.get("operatorId")
        if not operator_id:
            raise ReloadlyError("Operator ID missing after auto-detect.")

    url = f"{settings.RELOADLY_TOPUPS_BASE}/topups"
    payload = {
        "operatorId": int(operator_id),
        "amount": float(amount),
        "recipientPhone": {"countryCode": country_code, "number": phone},
        # "customIdentifier": "your-internal-id",  # optional
    }
    resp = requests.post(url, headers=_headers(token), json=payload, timeout=30)
    if not resp.ok:
        raise ReloadlyError(f"Top-up failed: {resp.status_code} {resp.text}")
    return resp.json()
