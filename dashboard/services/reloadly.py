# dashboard/services/reloadly.py
import time
from typing import Any, Dict, List, Optional

import requests
from django.conf import settings

# simple in-memory token cache
_token_cache = {"access_token": None, "exp": 0}


class ReloadlyError(Exception):
    pass


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": getattr(
            settings,
            "RELOADLY_ACCEPT_HEADER",
            "application/com.reloadly.topups-v1+json",
        ),
        "Content-Type": "application/json",
    }


def normalize_phone(phone: str, country_code: str = "PH") -> str:
    """
    Keep digits only. For PH:
      - Accepts '+63xxxxxxxxxx', '63xxxxxxxxxx', or '09xxxxxxxxx'
      - Returns 11-digit local format starting with '0' (e.g., '09171234567')
    """
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())

    if country_code.upper() == "PH":
        if digits.startswith("63"):
            digits = "0" + digits[2:]
        if len(digits) == 10 and digits.startswith("9"):
            digits = "0" + digits
        if len(digits) != 11 or not digits.startswith("0"):
            raise ReloadlyError("Invalid PH mobile number format.")
    elif not digits:
        raise ReloadlyError("Invalid phone number.")

    return digits


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
        "audience": settings.RELOADLY_AUDIENCE,  # should match TOPUPS base
    }
    try:
        resp = requests.post(url, json=payload, timeout=20)
        if not resp.ok:
            raise ReloadlyError(f"Auth failed: {resp.status_code} {resp.text}")
        data = resp.json()
    except requests.RequestException as e:
        raise ReloadlyError(f"Auth request error: {e}") from e

    _token_cache["access_token"] = data["access_token"]
    _token_cache["exp"] = time.time() + int(data.get("expires_in", 1800))
    return _token_cache["access_token"]


# -------- Reporting --------

def get_reloadly_balance() -> Dict[str, Any]:
    """GET /accounts/balance -> current Airtime wallet balance."""
    token = get_access_token()
    url = f"{settings.RELOADLY_TOPUPS_BASE}/accounts/balance"
    try:
        resp = requests.get(url, headers=_headers(token), timeout=15)
        resp.raise_for_status()
        return resp.json()  # includes balance, currencyCode, updatedAt
    except requests.RequestException as e:
        raise ReloadlyError(f"Balance request error: {e}") from e


def list_reloadly_transactions(page: int = 1, size: int = 20) -> List[Dict[str, Any]]:
    """GET /topups/reports/transactions -> list of top-up transactions (paginated)."""
    token = get_access_token()
    url = f"{settings.RELOADLY_TOPUPS_BASE}/topups/reports/transactions"
    params = {"page": page, "size": size}
    try:
        resp = requests.get(url, headers=_headers(token), params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "content" in data:
            return data["content"]
        return data if isinstance(data, list) else []
    except requests.RequestException as e:
        raise ReloadlyError(f"Transactions request error: {e}") from e


# -------- Operators / validation --------

def auto_detect_operator(phone: str, country_code: str = "PH") -> Dict[str, Any]:
    """GET operator by phone."""
    token = get_access_token()
    url = f"{settings.RELOADLY_TOPUPS_BASE}/operators/auto-detect/phone/{phone}/countries/{country_code}"
    try:
        resp = requests.get(url, headers=_headers(token), timeout=20)
        if not resp.ok:
            raise ReloadlyError(f"Auto-detect failed: {resp.status_code} {resp.text}")
        return resp.json()
    except requests.RequestException as e:
        raise ReloadlyError(f"Auto-detect request error: {e}") from e


def _validate_amount_against_operator(op: Dict[str, Any], amount: float) -> float:
    """
    Validates 'amount' with the operator rules:
      - If fixed amounts exist, amount must match one of them
      - Else must be within [minAmount, maxAmount]
    Returns rounded(amount) to 2 decimals.
    """
    amt = round(float(amount), 2)
    fixed = op.get("fixedAmounts") or []
    denom_type = (op.get("denominationType") or "").upper()
    min_amt = op.get("minAmount") or op.get("min_amount")
    max_amt = op.get("maxAmount") or op.get("max_amount")

    if fixed:
        fixed_rounded = {round(float(x), 2) for x in fixed}
        if amt not in fixed_rounded:
            raise ReloadlyError(
                f"Amount {amt} not in fixed denominations {sorted(fixed_rounded)}."
            )
        return amt

    # RANGE (or no fixed)
    if min_amt is not None and max_amt is not None:
        if not (float(min_amt) <= amt <= float(max_amt)):
            raise ReloadlyError(
                f"Amount {amt} outside allowed range [{min_amt}, {max_amt}]."
            )
    elif denom_type == "RANGE":
        # Extra guard if denom_type is RANGE but no bounds provided
        raise ReloadlyError("Operator requires a range amount, but limits were missing.")
    return amt


# -------- Topups --------

def send_topup(
    phone: str,
    amount: float,
    operator_id: Optional[int] = None,
    country_code: str = "PH",
    custom_identifier: Optional[str] = None,
) -> Dict[str, Any]:
    """
    POST /topups -> send mobile load.

    - Normalizes phone
    - Auto-detects operator if none provided
    - Validates amount (fixed vs range)
    - Adds optional customIdentifier for idempotency/audit
    """
    token = get_access_token()
    normalized = normalize_phone(phone, country_code=country_code)

    op = None
    if operator_id is None:
        op = auto_detect_operator(normalized, country_code)
        operator_id = op.get("operatorId")
        if not operator_id:
            raise ReloadlyError("Operator ID missing after auto-detect.")
    else:
        # If caller passed an operatorId anyway, try to validate using auto-detect context (best effort).
        try:
            op = auto_detect_operator(normalized, country_code)
        except Exception:
            op = None

    if op is not None:
        amount = _validate_amount_against_operator(op, amount)
    else:
        amount = round(float(amount), 2)

    url = f"{settings.RELOADLY_TOPUPS_BASE}/topups"
    payload: Dict[str, Any] = {
        "operatorId": int(operator_id),
        "amount": float(amount),
        "recipientPhone": {"countryCode": country_code, "number": normalized},
    }
    if custom_identifier:
        payload["customIdentifier"] = str(custom_identifier)

    try:
        resp = requests.post(url, headers=_headers(token), json=payload, timeout=30)
        if not resp.ok:
            raise ReloadlyError(f"Top-up failed: {resp.status_code} {resp.text}")
        return resp.json()
    except requests.RequestException as e:
        raise ReloadlyError(f"Top-up request error: {e}") from e
