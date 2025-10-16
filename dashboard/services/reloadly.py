# dashboard/services/reloadly.py
from __future__ import annotations
import time
import re
import threading
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests
from django.conf import settings

# ---------------- Token cache ----------------
_token_cache = {"access_token": None, "exp": 0}
_token_lock = threading.Lock()

class ReloadlyError(Exception):
    pass

DEFAULT_TIMEOUT = getattr(settings, "RELOADLY_HTTP_TIMEOUT", 20)

def _accept_header() -> str:
    # Keep your old default but allow override
    return getattr(
        settings,
        "RELOADLY_ACCEPT_HEADER",
        "application/com.reloadly.topups-v1+json",
    )

def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": _accept_header(),
        "Content-Type": "application/json",
    }

# ---------------- Auth ----------------
def get_access_token() -> str:
    """Get (and cache) a Reloadly OAuth token."""
    now = time.time()
    with _token_lock:
        if _token_cache["access_token"] and now < _token_cache["exp"] - 60:
            return _token_cache["access_token"]

        url = f"{settings.RELOADLY_AUTH_BASE}/oauth/token"
        payload = {
            "client_id": settings.RELOADLY_CLIENT_ID,
            "client_secret": settings.RELOADLY_CLIENT_SECRET,
            "grant_type": "client_credentials",
            "audience": settings.RELOADLY_AUDIENCE,
        }
        try:
            resp = requests.post(url, json=payload, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise ReloadlyError(f"Auth request error: {e}") from e

        _token_cache["access_token"] = data.get("access_token")
        _token_cache["exp"] = time.time() + int(data.get("expires_in", 1800))
        if not _token_cache["access_token"]:
            raise ReloadlyError("Auth failed: no access_token in response.")
        return _token_cache["access_token"]

def _request_with_refresh(method: str, url: str, **kwargs):
    """Do a request; if 401, refresh token once and retry."""
    token = get_access_token()
    headers = kwargs.pop("headers", {})
    headers.update(_headers(token))
    try:
        resp = requests.request(method, url, headers=headers, timeout=DEFAULT_TIMEOUT, **kwargs)
        if resp.status_code == 401:
            # force refresh and retry once
            with _token_lock:
                _token_cache["exp"] = 0
            token = get_access_token()
            headers = _headers(token)
            resp = requests.request(method, url, headers=headers, timeout=DEFAULT_TIMEOUT, **kwargs)
        return resp
    except requests.RequestException as e:
        raise ReloadlyError(str(e)) from e

# ---------------- Phone utils ----------------
def normalize_phone(phone: str, country_code: str = "PH") -> str:
    """
    Normalize to E.164. For PH:
      - Accept '+63XXXXXXXXXX', '63XXXXXXXXXX', '09XXXXXXXXX', '9XXXXXXXXX'
      - Return '+63XXXXXXXXXX'
    """
    raw = phone or ""
    digits = re.sub(r"[^\d+]", "", raw)

    if country_code.upper() == "PH":
        # Strip leading '+'
        if digits.startswith("+"):
            digits = digits[1:]

        # Cases: +63xxxxxxxxxx, 63xxxxxxxxxx, 09xxxxxxxxx, 9xxxxxxxxx
        if digits.startswith("63"):
            national = digits[2:]
        elif digits.startswith("0"):
            national = digits[1:]
        else:
            national = digits

        # Expect 10-digit national number (e.g. 917xxxxxxx)
        if not re.fullmatch(r"\d{10}", national):
            raise ReloadlyError("Invalid PH mobile number.")
        return f"+63{national}"

    # Fallback: require E.164 for non-PH
    if not raw.startswith("+"):
        raise ReloadlyError("Provide phone in E.164 format (+countrycode...).")
    return raw

# ---------------- Reporting ----------------
def get_reloadly_balance() -> Dict[str, Any]:
    url = f"{settings.RELOADLY_TOPUPS_BASE}/accounts/balance"
    resp = _request_with_refresh("GET", url)
    if not resp.ok:
        raise ReloadlyError(f"Balance request error: {resp.status_code} {resp.text}")
    data = resp.json()
    # Normalize to stable keys your views/template read
    balance = data.get("balance") or data.get("availableBalance") or data.get("amount")
    currency = data.get("currencyCode") or data.get("currency") or "PHP"
    return {"balance": balance, "currencyCode": currency}

def list_reloadly_transactions(page: int = 1, size: int = 20) -> Dict[str, Any]:
    """
    Normalize to {"content": [...]} so your view can do:
      tx = list_reloadly_transactions(...); tx.get("content")
    """
    # You were calling /topups/reports/transactions; keep that:
    url = f"{settings.RELOADLY_TOPUPS_BASE}/topups/reports/transactions"
    resp = _request_with_refresh("GET", url, params={"page": page, "size": size})
    if not resp.ok:
        raise ReloadlyError(f"Transactions request error: {resp.status_code} {resp.text}")
    data = resp.json()
    if isinstance(data, dict) and "content" in data:
        return data
    if isinstance(data, list):
        return {"content": data}
    # Fallback common shapes
    content = data.get("data") or data.get("items") or data.get("transactions") or []
    return {"content": content}

# ---------------- Operators ----------------
def auto_detect_operator(phone: str, country_code: str = "PH") -> Dict[str, Any]:
    """
    Use the **path** form (more widely supported):
      GET /operators/auto-detect/phone/{E164}/countries/{countryCode}
    """
    # Make sure it's E.164 (e.g., +639171234567)
    e164 = normalize_phone(phone, country_code=country_code)

    # The '+' must be URL-encoded; quote(..., safe="") encodes everything that needs it
    phone_segment = quote(e164, safe="")
    url = f"{settings.RELOADLY_TOPUPS_BASE}/operators/auto-detect/phone/{phone_segment}/countries/{country_code}"

    resp = _request_with_refresh("GET", url)
    if not resp.ok:
        raise ReloadlyError(f"Auto-detect failed: {resp.status_code} {resp.text}")

    data = resp.json()
    op = data[0] if isinstance(data, list) and data else data
    if not isinstance(op, dict) or not op.get("operatorId"):
        raise ReloadlyError("Unable to detect operator.")

    return {
        "operatorId": op.get("operatorId"),
        "name": op.get("name"),
        "fixedAmounts": op.get("fixedAmounts") or [],
        "minAmount": op.get("minAmount"),
        "maxAmount": op.get("maxAmount"),
        "denominationType": op.get("denominationType"),
        "supportsLocalAmounts": op.get("supportsLocalAmounts"),
    }

def _validate_amount_against_operator(op: Dict[str, Any], amount: float) -> float:
    """
    Validates 'amount' with the operator rules.
    Returns amount rounded to 2dp (float).
    """
    q = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    amt = float(q)
    fixed = op.get("fixedAmounts") or []
    denom_type = (op.get("denominationType") or "").upper()
    min_amt = op.get("minAmount")
    max_amt = op.get("maxAmount")

    if fixed:
        fixed_rounded = {float(Decimal(str(x)).quantize(Decimal("0.01"))) for x in fixed}
        if amt not in fixed_rounded:
            raise ReloadlyError(f"Amount {amt:.2f} not in fixed denominations {sorted(fixed_rounded)}.")
        return amt

    if min_amt is not None and max_amt is not None:
        if not (float(min_amt) <= amt <= float(max_amt)):
            raise ReloadlyError(f"Amount {amt:.2f} outside allowed range [{min_amt}, {max_amt}].")
    elif denom_type == "RANGE":
        raise ReloadlyError("Operator requires a range amount, but limits were missing.")
    return amt

# ---------------- Topups ----------------
# services/reloadly.py  (only the send_topup function body changes)

def send_topup(
    *,
    phone: str,
    amount: float,
    operator_id: int,
    custom_identifier: Optional[str] = None,
) -> Dict[str, Any]:
    """
    POST /topups
    Body must include recipientPhone as an object and useLocalAmount=True
    when the amount is in the destination currency (PHP).
    """
    normalized = normalize_phone(phone, country_code="PH")      # "+63917XXXXXXX"
    # Reloadly wants countryCode + number (no '+')
    number_no_plus = normalized.lstrip("+")

    # (Optional) pre-validate against operator limits — keep as-is if you like
    try:
        op = auto_detect_operator(normalized, country_code="PH")
        amount = _validate_amount_against_operator(op, amount)
    except ReloadlyError:
        pass

    url = f"{settings.RELOADLY_TOPUPS_BASE}/topups"
    payload: Dict[str, Any] = {
        "operatorId": int(operator_id),
        "amount": float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "useLocalAmount": True,                 # <-- IMPORTANT for PHP amounts
        "recipientPhone": {                     # <-- MUST be an object
            "countryCode": "PH",
            "number": number_no_plus           # e.g. "63917XXXXXXX"
        },
    }
    if custom_identifier:
        payload["customIdentifier"] = str(custom_identifier)

    resp = _request_with_refresh("POST", url, json=payload)
    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise ReloadlyError(f"Top-up failed: {resp.status_code} {detail}")

    data = resp.json() if resp.text.strip() else {}
    data["status"] = (data.get("status") or data.get("transactionStatus") or "").upper() or "PENDING"
    return data
