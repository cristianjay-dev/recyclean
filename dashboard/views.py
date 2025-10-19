# views.py — Recyclean (auth, submissions, rewards, DIY CRUD + daily rotation + summary CSVs)
from __future__ import annotations

import csv
import io
import json
import logging
import secrets

from django.contrib import messages
import re
from datetime import datetime, timedelta, date
from functools import wraps
from typing import List, Dict, Tuple
from collections import Counter
import cv2
import numpy as np
from django.urls import reverse
from django.conf import settings
from django.contrib.auth import authenticate, logout, get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import Sum, Min, Max
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.html import escape
from django.utils.timezone import localtime
from django.views import View
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_POST, require_http_methods
from rest_framework import permissions, serializers, views
from rest_framework.decorators import api_view, permission_classes, parser_classes, authentication_classes
from rest_framework.authtoken.models import Token
from rest_framework.authentication import TokenAuthentication, SessionAuthentication, BasicAuthentication
from rest_framework.permissions import BasePermission
from rest_framework.parsers import JSONParser, FormParser, MultiPartParser

from rest_framework.response import Response
from urllib.parse import urlparse, parse_qs
from decimal import Decimal, ROUND_HALF_UP

from .forms import DIYTutorialForm
from .models import (
    Barangay,
    DropOffSite,
    RewardRequest,
    StaffApprovalRequest,
    StaffTransaction,
    Submission,
    User,
    UserPointsLedger,
    DIYTutorial,
    DIYDailyPool,
    DIYDailySelection,
    DIYSubmission,
    PointsConfig,
)

from .services.reloadly import (
    get_reloadly_balance,
    list_reloadly_transactions,
    send_topup,
    auto_detect_operator,
    normalize_phone,
    ReloadlyError,
)

LOGGER = logging.getLogger(__name__)
COUNTRY = getattr(settings, "RELOADLY_COUNTRY_CODE", "PH")
TWO_DP = Decimal("0.01")



# ==============================================================================
# Utilities
# ==============================================================================

# --- DropOffSite helpers ---

def ensure_site_and_add_staff(barangay, staff_user: User):
    """
    Idempotently ensure a DropOffSite exists for the barangay,
    and add the staff user to that site's staff_members.
    """
    if not barangay or not staff_user:
        return
    site, _ = DropOffSite.objects.get_or_create(barangay=barangay)
    site.staff_members.add(staff_user)
    # keep user's barangay in sync
    if staff_user.barangay_id != barangay.id:
        staff_user.barangay = barangay
        staff_user.save(update_fields=["barangay"])


def remove_staff_from_all_sites(staff_user: User):
    """
    Detach staff from any sites they’re assigned to.
    """
    if not staff_user:
        return
    for site in DropOffSite.objects.filter(staff_members=staff_user):
        site.staff_members.remove(staff_user)


def _is_real_staff(user) -> bool:
    return bool(
        user
        and user.is_authenticated
        and user.is_active
        and user.is_approved
        and user.is_staff
        and user.groups.filter(name="staff").exists()
    )



  # add this

def require_staff_page(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if _admin_bypass_ok(request):
            return view_func(request, *args, **kwargs)

        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?next={request.get_full_path()}")

        if not _is_real_staff(request.user):
            messages.error(request, "Staff approval required to access that page.")
            logout(request)  # ← key: prevents redirect loop with LoginView
            return redirect("login")

        return view_func(request, *args, **kwargs)
    return _wrapped


def php_to_points(php_amount: int) -> int:
    rate = int(getattr(settings, "POINTS_PER_PHP", 10))
    return int(php_amount) * rate

def points_to_php_decimal(points: int) -> Decimal:
    rate = int(getattr(settings, "POINTS_PER_PHP", 10))
    return (Decimal(points) / Decimal(rate)).quantize(TWO_DP, rounding=ROUND_HALF_UP)


def get_or_create_group(name: str) -> Group:
    grp, _ = Group.objects.get_or_create(name=name)
    return grp


def compute_points(bottle_data: List[Dict]) -> int:
    """
    Compute points for bottles using PointsConfig (small/large only).
    bottle_data item shape: {"size": "small|large", "count": int}
    """
    cfg = PointsConfig.current()
    size_points = {"small": cfg.small_bottle_points, "large": cfg.large_bottle_points}
    total = 0
    for item in bottle_data or []:
        size = (item.get("size") or "").lower()
        try:
            count = int(item.get("count", 0))
        except Exception:
            count = 0
        if size in size_points and count >= 0:
            total += size_points[size] * count
    return total

# Put near your other utils in views.py


_URL_RE = re.compile(r'(https?://[^\s<>"\']+)', re.IGNORECASE)
_BULLET_RE = re.compile(r'^\s*[-*•]\s+')
_NUMBER_RE = re.compile(r'^\s*\d+[.)]\s+')

def _linkify_escaped_text(s: str) -> str:
    """
    Given an already-escaped string, wrap URLs with <a href="...">...</a>.
    """
    def _wrap(m):
        url = m.group(1)
        return f'<a href="{url}" target="_blank" rel="noopener">{url}</a>'
    return _URL_RE.sub(_wrap, s)

def _render_bullets_or_paragraph(text: str) -> str:
    """
    Block-aware formatter:
    - Blank-line separated blocks become separate lists/paragraphs
    - Lines starting with -, *, • → <ul>
    - Lines starting with 1. / 2) … → <ol>
    - Otherwise keep line breaks as <br/>
    - Always HTML-escape, then linkify URLs
    """
    text = (text or "").strip()
    if not text:
        return ""

    html_parts = []
    # Split into blocks by blank lines to preserve paragraphs
    blocks = [b for b in re.split(r'\r?\n\s*\r?\n', text) if b.strip()]

    for block in blocks:
        lines = [ln.rstrip() for ln in block.splitlines() if ln.strip()]
        has_bullets = any(_BULLET_RE.match(ln) for ln in lines)
        has_numbers = any(_NUMBER_RE.match(ln) for ln in lines)

        if has_numbers and not has_bullets:
            items = []
            for ln in lines:
                clean = _NUMBER_RE.sub('', ln, count=1).strip()
                esc = escape(clean)
                items.append(f"<li>{_linkify_escaped_text(esc)}</li>")
            html_parts.append(f"<ol>{''.join(items)}</ol>")
        elif has_bullets:
            items = []
            for ln in lines:
                clean = _BULLET_RE.sub('', ln, count=1).strip()
                esc = escape(clean)
                items.append(f"<li>{_linkify_escaped_text(esc)}</li>")
            html_parts.append(f"<ul>{''.join(items)}</ul>")
        else:
            # Plain paragraph: keep line breaks
            esc = escape(block)
            esc = _linkify_escaped_text(esc)
            esc = esc.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
            html_parts.append(f"<p>{esc}</p>")

    return "".join(html_parts)


def today_ph():
    return timezone.localdate()  # TIME_ZONE="Asia/Manila"


# views.py (replace your current ensure_unique_username with this)


USERNAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{2,19}$')

def ensure_unique_username(base_username: str) -> str:
    """
    Normalize a desired username to pass the model validator and be unique (case-insensitive).
    Rules: start with a letter; [A-Za-z0-9_]; length 3–20. If not valid, coerce.
    """
    base = (base_username or "").strip() or "user"
    # lowercase + replace invalid with '_'
    norm = re.sub(r'[^A-Za-z0-9_]', '_', base).lower() or "user"
    if not norm[0].isalpha():
        norm = f"u{norm}"
    if len(norm) < 3:
        norm = (norm + "___")[:3]
    if len(norm) > 20:
        norm = norm[:20]
    if not USERNAME_RE.match(norm):
        norm = "user"
    candidate = norm
    i = 1
    while User.objects.filter(username__iexact=candidate).exists():
        candidate = f"{norm[:18]}{i}"
        i += 1
    return candidate


# views.py (add)

@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def list_barangays(request):
    """
    GET /api/barangays/
    → {"success": true, "barangays": [{"id": 1, "name": "...", "city": "Tacloban City"}, ...]}
    """
    qs = Barangay.objects.all().order_by("name")
    data = [{"id": b.id, "name": b.name, "city": b.city} for b in qs]
    return Response({"success": True, "barangays": data, "items": data})

@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def username_available(request):
    """
    GET /api/utils/username-available/?username=foo
    -> {"success": true, "available": true|false, "suggestion": "foo1"}
    """
    desired = (request.GET.get("username") or "").strip()
    if not desired:
        return Response({"success": False, "error": "Missing username."}, status=400)

    exists = User.objects.filter(username__iexact=desired).exists()
    suggestion = None
    if exists:
        suggestion = ensure_unique_username(desired)

    return Response({"success": True, "available": not exists, "suggestion": suggestion})


# ---- Admin/staff guard with development + shared-key bypass ------------------

# --- Admin-only helpers (superuser only) ---

def _is_admin_only(user) -> bool:
    return bool(user and user.is_authenticated and user.is_active and user.is_superuser)

def require_admin_page(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if _admin_bypass_ok(request):
            return view_func(request, *args, **kwargs)

        if not request.user.is_authenticated:
            return redirect(f"{reverse('login')}?next={request.get_full_path()}")

        if not _is_admin_only(request.user):
            messages.error(request, "Admin access required.")
            logout(request)                      # prevents redirect loops
            return redirect("login")

        return view_func(request, *args, **kwargs)
    return _wrapped


def require_admin_json(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if _admin_bypass_ok(request):
            return view_func(request, *args, **kwargs)
        u = request.user
        if not u.is_authenticated:
            return JsonResponse({"success": False, "error": "Authentication required."}, status=401)
        if not _is_admin_only(u):
            return JsonResponse({"success": False, "error": "Admin access required."}, status=403)
        return view_func(request, *args, **kwargs)
    return _wrapped


class IsAdminOnly(BasePermission):
    def has_permission(self, request, view):
        if _admin_bypass_ok(request):
            return True
        return _is_admin_only(request.user)

# views.py
def _admin_bypass_ok(request) -> bool:
    """
    Allow ONLY:
      - a matching ADMIN_SHARED_KEY, or
      - DIY_ADMIN_BYPASS=true (explicit), never DEBUG.
    """
    from django.conf import settings

    shared = getattr(settings, "ADMIN_SHARED_KEY", None)
    if shared:
        supplied = (
            request.headers.get("X-Admin-Key")
            or request.POST.get("admin_key")
            or request.GET.get("admin_key")
        )
        if supplied and str(supplied) == str(shared):
            return True

    return bool(getattr(settings, "DIY_ADMIN_BYPASS", False))




def require_staff_json(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if _admin_bypass_ok(request):
            return view_func(request, *args, **kwargs)
        u = request.user
        if not u.is_authenticated:
            return JsonResponse({"success": False, "error": "Authentication required."}, status=401)
        if not _is_real_staff(u):
            return JsonResponse({"success": False, "error": "Staff permission required."}, status=403)
        return view_func(request, *args, **kwargs)
    return _wrapped


class IsStaffish(BasePermission):
    def has_permission(self, request, view):
        if _admin_bypass_ok(request):
            return True
        return _is_real_staff(request.user)


@require_POST
@require_admin_json
def approve_staff_json(request, user_id: int):
    staff = get_object_or_404(User, pk=user_id)
    req = StaffApprovalRequest.objects.filter(user=staff, status="pending").first()

    # Decide which barangay to bind:
    barangay = req.requested_barangay if (req and req.requested_barangay) else staff.barangay
    if not barangay:
        return JsonResponse({"success": False, "error": "Requested barangay is required."}, status=400)

    with transaction.atomic():
        # Approve + mark as staff
        staff.is_approved = True
        staff.is_active = True
        staff.is_staff = True
        staff.save(update_fields=["is_approved", "is_active", "is_staff"])

        # Ensure group
        staff_group = get_or_create_group("staff")
        staff.groups.add(staff_group)

        # Ensure site + attach staff (and sync user's barangay)
        ensure_site_and_add_staff(barangay, staff)

        if req:
            req.status = "approved"
            req.decided_by = request.user if request.user.is_authenticated else None
            req.decided_at = timezone.now()
            req.save(update_fields=["status", "decided_by", "decided_at"])

    return JsonResponse({"success": True})



@require_POST
@require_admin_json
def reject_staff_json(request, user_id: int):
    staff = get_object_or_404(User, pk=user_id)
    with transaction.atomic():
        req = StaffApprovalRequest.objects.filter(user=staff, status="pending").first()
        if req:
            req.status = "rejected"
            req.decided_by = request.user if request.user.is_authenticated else None
            req.decided_at = timezone.now()
            req.save(update_fields=["status", "decided_by", "decided_at"])

        remove_staff_from_all_sites(staff)
        staff.delete()

    return JsonResponse({"success": True})


# ==============================================================================
# Serializers
# ==============================================================================


# --- Profile serializers ---
class MeUpdateSerializer(serializers.Serializer):
    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name  = serializers.CharField(required=False, allow_blank=True)
    email      = serializers.EmailField(required=False)
    mobile     = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs):
        user: User = self.context["request"].user
        email  = attrs.get("email")
        mobile = attrs.get("mobile")
        if email and User.objects.filter(email__iexact=email).exclude(id=user.id).exists():
            raise serializers.ValidationError("Email already in use.")
        if mobile and User.objects.filter(mobile_number=mobile).exclude(id=user.id).exists():
            raise serializers.ValidationError("Mobile already in use.")
        return attrs


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField()
    new_password = serializers.CharField()

    def validate(self, attrs):
        user: User = self.context["request"].user
        if not user.check_password(attrs["old_password"]):
            raise serializers.ValidationError("Old password is incorrect.")
        if len(attrs["new_password"]) < 8:
            raise serializers.ValidationError("New password must be at least 8 characters.")
        return attrs



class StaffSignupSerializer(serializers.Serializer):
    username = serializers.CharField(required=False, allow_blank=True)
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    mobile = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    barangay_id = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        email = attrs["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("Email already in use.")
        mobile = attrs.get("mobile")
        if mobile and User.objects.filter(mobile_number=mobile).exists():
            raise serializers.ValidationError("Mobile already in use.")
        username = (attrs.get("username") or "").strip()
        if username and User.objects.filter(username__iexact=username).exists():
            raise serializers.ValidationError("Username already taken.")
        return attrs


class StaffLoginSerializer(serializers.Serializer):
    identifier = serializers.CharField()  # username | email | mobile
    password = serializers.CharField(write_only=True)


class ResidentSignupSerializer(serializers.Serializer):
    username = serializers.CharField(required=False, allow_blank=True)
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    mobile = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    barangay_id = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        email = attrs["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("Email already in use.")
        mobile = attrs.get("mobile")
        if mobile and User.objects.filter(mobile_number=mobile).exists():
            raise serializers.ValidationError("Mobile already in use.")
        username = (attrs.get("username") or "").strip()
        if username and User.objects.filter(username__iexact=username).exists():
            raise serializers.ValidationError("Username already taken.")
        return attrs


class ResidentLoginSerializer(serializers.Serializer):
    identifier = serializers.CharField()  # username | email
    password = serializers.CharField(write_only=True)


class SubmissionIntakeSerializer(serializers.Serializer):
    dropoff_site_id = serializers.IntegerField()
    bottle_data = serializers.ListField(child=serializers.DictField(), allow_empty=False)

    def validate_bottle_data(self, value):
        for item in value:
            size = (item.get("size") or "").lower()
            if size not in {"small", "large"}:
                raise serializers.ValidationError("Invalid size; use small/large.")
            try:
                count = int(item.get("count"))
            except Exception:
                raise serializers.ValidationError("count must be an integer.")
            if count < 0:
                raise serializers.ValidationError("count must be >= 0.")
        return value


class SubmissionClaimSerializer(serializers.Serializer):
    qr_token = serializers.CharField()


class DIYSubmitSerializer(serializers.Serializer):
    tutorial_id = serializers.IntegerField()
    image = serializers.ImageField()
    caption = serializers.CharField(required=False, allow_blank=True)
    is_public = serializers.BooleanField(required=False, default=True)


# ==============================================================================
# Dashboard & Reward Requests (server-rendered)
# ==============================================================================
@require_admin_page
def dashboard(request):
    reloadly_balance = None
    reloadly_currency_code = "PHP"
    reloadly_currency_symbol = "₱"
    reloadly_error = None
    reloadly_updated_at = None
    try:
        bal = get_reloadly_balance()
        if isinstance(bal, dict):
            reloadly_balance = bal.get("balance") or bal.get("availableBalance") or bal.get("amount") or None
            reloadly_currency_code = bal.get("currencyCode") or bal.get("currency") or "PHP"
        else:
            reloadly_balance = bal
        reloadly_updated_at = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        reloadly_error = f"Unable to fetch Reloadly balance: {e}"
        LOGGER.warning(reloadly_error)

    context = {
        "total_users": User.objects.count(),
        "total_submissions": Submission.objects.count(),
        "total_rewards": RewardRequest.objects.count(),
        "reloadly_balance": reloadly_balance,
        "reloadly_currency": reloadly_currency_code,
        "reloadly_currency_code": reloadly_currency_code,
        "reloadly_currency_symbol": reloadly_currency_symbol,
        "reloadly_error": reloadly_error,
        "reloadly_updated_at": reloadly_updated_at,
    }
    return render(request, "dashboard.html", context)

@require_admin_page
def reward_requests_view(request):
    reward_requests = RewardRequest.objects.select_related("user").order_by("-id")
    reloadly_balance = None
    reloadly_currency = "PHP"
    reloadly_error = None
    reloadly_txns = []
    try:
        bal = get_reloadly_balance()
        if isinstance(bal, dict):
            reloadly_balance = bal.get("balance") or bal.get("availableBalance") or bal.get("amount")
            reloadly_currency = bal.get("currencyCode") or bal.get("currency") or "PHP"
        else:
            reloadly_balance = bal
    except Exception as e:
        reloadly_error = f"Unable to fetch Reloadly balance: {e}"
    try:
        tx = list_reloadly_transactions(page=1, size=20)
        reloadly_txns = (tx.get("content") or tx.get("data") or tx.get("transactions") or tx.get("items") or []) if isinstance(tx, dict) else (tx or [])
    except Exception as e:
        reloadly_error = (reloadly_error + f" | Txns error: {e}") if reloadly_error else f"Txns error: {e}"

    context = {
        "reward_requests": reward_requests,
        "total_rewards": RewardRequest.objects.count(),
        "reloadly_balance": reloadly_balance,
        "reloadly_currency": reloadly_currency,
        "reloadly_error": reloadly_error,
        "reloadly_txns": reloadly_txns,
    }
    return render(request, "reward_requests.html", context)


# Public API to normalize PH numbers for clients
@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def normalize_phone_ph(request):
    phone = request.GET.get("phone") or (getattr(request, "query_params", {}) or {}).get("phone")
    if not phone:
        return Response({"success": False, "error": "Missing phone."}, status=400)
    try:
        normalized = normalize_phone(phone, country_code="PH")   # <-- use shared helper
    except ReloadlyError as e:
        return Response({"success": False, "error": str(e)}, status=400)
    return Response({"success": True, "phone": normalized})




@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication, BasicAuthentication])
@permission_classes([permissions.IsAuthenticated])
def user_history(request, user_id: int):
    """
    GET /api/user/<id>/history/
    Returns a single merged list (newest first) of:
      - submission_claim (points + submission id)
      - diy_submit (points + tutorial title when available)
      - reward (Reloadly top-ups with status)
    """
    user = get_object_or_404(User, pk=user_id)
    if user.id != request.user.id and not _admin_bypass_ok(request):
        return Response({"success": False, "error": "Forbidden."}, status=403)

    items = []

    # --- Points ledger (submission claims + DIY) ---
    for led in (
        UserPointsLedger.objects
        .filter(user=user)
        .select_related("submission")
        .order_by("-id")[:200]   # cap for performance; adjust as you like
    ):
        # Map source → type/label
        src = (led.source or "").lower()
        if src == "submission_claim":
            item_type = "points_earned"
            title = "QR Claim"
            subtitle = f"+{led.delta_points} pts"
            extra = {"submission_id": getattr(led.submission, "id", None)}
        elif src == "diy_submit":
            item_type = "points_earned"
            title = "DIY Submission"
            subtitle = f"+{led.delta_points} pts"
            extra = {"notes": led.notes}
        else:
            # skip internal adjustments unless you want to show them too
            item_type = "adjustment"
            title = led.notes or "Points Update"
            subtitle = f"{led.delta_points:+} pts"
            extra = {}

        items.append({
            "type": item_type,
            "title": title,
            "subtitle": subtitle,
            "points_delta": led.delta_points,
            "balance_after": led.balance_after,
            "status": None,
            "amount_php": None,
            "phone": None,
            "ts": localtime(getattr(led, "created_at", None) or timezone.now()).isoformat(),
            "extra": extra,
        })

    # --- Rewards (Reloadly) ---
    for rr in RewardRequest.objects.filter(user=user).order_by("-id")[:200]:
        items.append({
            "type": "reward",
            "title": f"Load to {rr.mobile_number}",
            "subtitle": f"₱{rr.amount} • {rr.operator_name or rr.telco or ''}".strip(),
            "points_delta": -int(rr.points_used or 0),
            "balance_after": None,   # we don’t always adjust here (might be webhook)
            "status": rr.status,     # requested | paid | rejected | ...
            "amount_php": str(rr.amount or ""),
            "phone": rr.mobile_number,
            "ts": localtime(rr.date_requested or rr.created_at or timezone.now()).isoformat(),
            "extra": {"reloadly_tx_id": rr.reloadly_tx_id},
        })

    # (Optional) You could also include raw Submissions or DIYSubmission rows here
    # if you want even more detail.

    # sort newest → oldest by timestamp
    items.sort(key=lambda x: x["ts"], reverse=True)

    return Response({"success": True, "items": items})



# ==============================================================================
# DIY management (server page + APIs your template uses)
# ==============================================================================
@require_admin_page
@ensure_csrf_cookie
def diy_dashboard(request):
    form = DIYTutorialForm()
    tutorials = DIYTutorial.objects.order_by("-created_at")
    return render(request, "diy_dashboard.html", {"form": form, "tutorials": tutorials})


# views.py

DAILY_COUNT_DEFAULT = getattr(settings, "DIY_DAILY_COUNT", 3)

def _abs_or_none(request, f):
    return request.build_absolute_uri(f.url) if f else None


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
@authentication_classes([TokenAuthentication, SessionAuthentication, BasicAuthentication])
def api_diy_daily(request):
    date_val = today_ph()
    desired_count = int(request.GET.get("count") or DAILY_COUNT_DEFAULT)

    # NEW: allow ?refresh=1 (or true/yes) to re-pick for today
    refresh = str(request.GET.get("refresh", "0")).lower() in {"1", "true", "yes"}

    # If refreshing, clear today's selections so we can repopulate
    if refresh:
        DIYDailySelection.objects.filter(date=date_val).delete()

    existing = (
        DIYDailySelection.objects
        .filter(date=date_val)
        .select_related("tutorial")
        .order_by("id")
    )

    # Only return early if we already have enough and we're NOT refreshing
    if not refresh and existing.count() >= desired_count:
        tutorials = [e.tutorial for e in existing[:desired_count]]
        return Response({
            "date": str(date_val),
            "tutorials": [
                {
                    "id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "description_html": _render_bullets_or_paragraph(t.description),
                    "video_url": t.video_url,
                    "thumbnail": _abs_or_none(request, t.thumbnail) or _youtube_thumb(t.video_url),
                    "points_on_submit": t.points_on_submit,
                    "has_submitted": (
                        request.user.is_authenticated
                        and DIYSubmission.objects.filter(user=request.user, tutorial=t).exists()
                    ),
                }
                for t in tutorials
            ],
        })

    # Build/refresh the pool for today
    pool, _ = DIYDailyPool.objects.get_or_create(date=date_val)

    # NEW: keep today's pool in sync with ALL active tutorials
    active_ids = set(DIYTutorial.objects.filter(is_active=True).values_list("id", flat=True))
    pool_ids = set(pool.tutorials.values_list("id", flat=True))
    missing_ids = active_ids - pool_ids
    if missing_ids:
        pool.tutorials.add(*missing_ids)

    # ---- the rest of your function stays the same from here ----
    # Recent-repeat window
    no_repeat_days = int(getattr(settings, "DIY_NO_REPEAT_DAYS", 5))
    window_start = date_val - timedelta(days=max(no_repeat_days - 1, 0))
    recent_ids = set(
        DIYDailySelection.objects
        .filter(date__gte=window_start)
        .values_list("tutorial_id", flat=True)
        .distinct()
    )

    base_qs = pool.tutorials.filter(is_active=True)
    candidates_excl = list(base_qs.exclude(id__in=recent_ids).order_by("id").values_list("id", flat=True))
    candidates_all = list(base_qs.order_by("id").values_list("id", flat=True))

    if candidates_excl:
        ids = candidates_excl
    else:
        ids = candidates_all

    if not ids:
        return Response(status=204)

    salt = int(getattr(settings, "DIY_ROTATION_SALT", 0))
    start_idx = (date_val.toordinal() + salt) % len(ids)

    picked_ids_today = set(existing.values_list("tutorial_id", flat=True))
    todays_ids = []
    i = 0
    while len(todays_ids) < desired_count and i < len(ids) * 2:
        tid = ids[(start_idx + i) % len(ids)]
        if tid not in todays_ids and tid not in picked_ids_today:
            todays_ids.append(tid)
        i += 1

    if len(todays_ids) < desired_count:
        for tid in candidates_all:
            if len(todays_ids) >= desired_count:
                break
            if tid not in todays_ids and tid not in picked_ids_today:
                todays_ids.append(tid)

    for tid in todays_ids:
        DIYDailySelection.objects.get_or_create(
            date=date_val,
            tutorial_id=tid,
            defaults={"pool": pool},
        )

    final = (
        DIYDailySelection.objects
        .filter(date=date_val)
        .select_related("tutorial")
        .order_by("id")
    )[:desired_count]

    tutorials = [e.tutorial for e in final]
    return Response({
        "date": str(date_val),
        "tutorials": [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "description_html": _render_bullets_or_paragraph(t.description),
                "video_url": t.video_url,
                "thumbnail": _abs_or_none(request, t.thumbnail) or _youtube_thumb(t.video_url),
                "points_on_submit": t.points_on_submit,
                "has_submitted": (
                    request.user.is_authenticated
                    and DIYSubmission.objects.filter(user=request.user, tutorial=t).exists()
                ),
            }
            for t in tutorials
        ],
    })


# --- helpers near your other utils ---

# Re-seed a specific date after a featured tutorial was removed/deleted.
def _reseed_for_date(date_val, request=None, desired_count=None):
    if desired_count is None:
        desired_count = int(getattr(settings, "DIY_DAILY_COUNT", 3))

    # Ensure a pool exists for that day
    pool, _ = DIYDailyPool.objects.get_or_create(date=date_val)

    # Keep the pool in sync with *current* active tutorials:
    active_ids = set(DIYTutorial.objects.filter(is_active=True).values_list("id", flat=True))
    pool_ids = set(pool.tutorials.values_list("id", flat=True))
    to_add = active_ids - pool_ids
    to_remove = pool_ids - active_ids
    if to_add:
        pool.tutorials.add(*to_add)
    if to_remove:
        pool.tutorials.remove(*to_remove)

    # Already-selected (after we may have deleted some)
    existing = (
        DIYDailySelection.objects
        .filter(date=date_val)
        .select_related("tutorial")
        .order_by("id")
    )
    if existing.count() >= desired_count:
        return  # nothing to do

    # Recent-repeat window relative to that date
    no_repeat_days = int(getattr(settings, "DIY_NO_REPEAT_DAYS", 5))
    window_start = date_val - timedelta(days=max(no_repeat_days - 1, 0))
    recent_ids = set(
        DIYDailySelection.objects
        .filter(date__gte=window_start)
        .values_list("tutorial_id", flat=True)
        .distinct()
    )

    base_qs = pool.tutorials.filter(is_active=True)
    candidates_excl = list(
        base_qs.exclude(id__in=recent_ids).order_by("id").values_list("id", flat=True)
    )
    candidates_all = list(base_qs.order_by("id").values_list("id", flat=True))

    ids = candidates_excl if candidates_excl else candidates_all
    if not ids:
        return  # nothing available to seed

    salt = int(getattr(settings, "DIY_ROTATION_SALT", 0))
    start_idx = (date_val.toordinal() + salt) % len(ids)

    picked_ids_today = set(existing.values_list("tutorial_id", flat=True))
    todays_ids = []
    i = 0
    while len(todays_ids) < desired_count and i < len(ids) * 2:
        tid = ids[(start_idx + i) % len(ids)]
        if tid not in todays_ids and tid not in picked_ids_today:
            todays_ids.append(tid)
        i += 1

    if len(todays_ids) < desired_count:
        for tid in candidates_all:
            if len(todays_ids) >= desired_count:
                break
            if tid not in todays_ids and tid not in picked_ids_today:
                todays_ids.append(tid)

    for tid in todays_ids:
        DIYDailySelection.objects.get_or_create(
            date=date_val,
            tutorial_id=tid,
            defaults={"pool": pool},
        )

def _youtube_id(url: str) -> str | None:
    try:
        p = urlparse(url)
        if p.netloc in {"youtu.be"}:
            # https://youtu.be/<id>
            return p.path.lstrip("/") or None
        if "youtube.com" in p.netloc:
            if p.path == "/watch":
                return (parse_qs(p.query).get("v") or [None])[0]
            # /embed/<id> or /shorts/<id> or /v/<id>
            parts = p.path.strip("/").split("/")
            if parts and parts[0] in {"embed", "shorts", "v"} and len(parts) > 1:
                return parts[1]
        return None
    except Exception:
        return None

def _youtube_thumb(url: str) -> str | None:
    vid = _youtube_id(url or "")
    if not vid:
        return None
    # 'maxresdefault.jpg' sometimes 404s; 'hqdefault.jpg' is reliable
    return f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"


# Back-compat alias
@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def diy_daily(request):
    return api_diy_daily(request)


@require_POST
@require_admin_json
def diy_feature_today(request):
    try:
        tutorial_id = int(request.POST.get("tutorial_id", "0"))
        date_str = request.POST.get("date") or ""
        if not tutorial_id or not date_str:
            return JsonResponse({"success": False, "error": "Missing tutorial_id or date."}, status=400)

        try:
            picked_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return JsonResponse({"success": False, "error": "Invalid date format, use YYYY-MM-DD."}, status=400)

        tutorial = get_object_or_404(DIYTutorial, pk=tutorial_id)
        pool, _ = DIYDailyPool.objects.get_or_create(date=picked_date)
        pool.tutorials.add(tutorial)

        DIYDailySelection.objects.get_or_create(
            date=picked_date,
            tutorial=tutorial,
            defaults={"pool": pool},
        )
        return JsonResponse({"success": True})
    except Exception as e:
        LOGGER.exception("Feature DIY error")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@require_POST
@require_admin_json
def diy_create_tutorial(request):
    form = DIYTutorialForm(request.POST, request.FILES)
    if form.is_valid():
        t = form.save()
        return JsonResponse({"success": True, "id": t.id})
    return JsonResponse({"success": False, "errors": form.errors}, status=400)


@require_POST
@require_admin_json
def diy_update_tutorial(request, tutorial_id: int):
    t = get_object_or_404(DIYTutorial, pk=tutorial_id)
    form = DIYTutorialForm(request.POST, request.FILES, instance=t)
    if form.is_valid():
        form.save()
        return JsonResponse({"success": True})
    return JsonResponse({"success": False, "errors": form.errors}, status=400)


@require_POST
@require_admin_json
def diy_delete_tutorial(request, tutorial_id: int):
    """
    Delete a DIY tutorial even if it was featured; automatically remove the selections
    that reference it and re-seed those dates to keep the daily count.
    Also removes the tutorial from all daily pools before deleting it.
    """
    with transaction.atomic():
        t = get_object_or_404(DIYTutorial, pk=tutorial_id)

        # Collect all dates where this tutorial was featured
        affected_dates = list(
            DIYDailySelection.objects
            .filter(tutorial=t)
            .values_list("date", flat=True)
            .distinct()
        )
        affected_dates.sort()

        # Remove from all pools (M2M) to avoid dangling references
        for pool in DIYDailyPool.objects.filter(tutorials=t):
            pool.tutorials.remove(t)

        # Remove any selections referring to this tutorial
        DIYDailySelection.objects.filter(tutorial=t).delete()

        # Now delete the tutorial itself
        t.delete()

    # Re-seed affected dates outside the transaction (or inside, both are fine)
    desired_count = int(getattr(settings, "DIY_DAILY_COUNT", 3))
    for d in affected_dates:
        _reseed_for_date(d, request=request, desired_count=desired_count)

    return JsonResponse({"success": True, "reseeded_dates": [str(d) for d in affected_dates]})




# ---- DIY submission (mobile/user uploads proof) ----
class DIYSubmitView(views.APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request):
        ser = DIYSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user: User = request.user
        tutorial = get_object_or_404(DIYTutorial, pk=ser.validated_data["tutorial_id"])

        existing = DIYSubmission.objects.filter(user=user, tutorial=tutorial).first()
        if existing:
            return Response(
                {"success": True, "message": "Already submitted for this DIY.", "points_awarded": existing.points_awarded},
                status=200,
            )

        with transaction.atomic():
            sub = DIYSubmission.objects.create(
                user=user,
                tutorial=tutorial,
                image=ser.validated_data["image"],
                caption=ser.validated_data.get("caption") or "",
                is_public=ser.validated_data.get("is_public", True),
                approved=True,
                points_awarded=tutorial.points_on_submit or 0,
                awarded_at=timezone.now() if (tutorial.points_on_submit or 0) > 0 else None,
            )

            if sub.points_awarded > 0:
                user.total_points += sub.points_awarded
                user.save(update_fields=["total_points"])
                UserPointsLedger.objects.create(
                    user=user,
                    submission=None,
                    source="diy_submit",
                    delta_points=sub.points_awarded,
                    balance_after=user.total_points,
                    notes=f"DIY: {tutorial.title}",
                )

        return Response({"success": True, "points_awarded": sub.points_awarded, "balance": user.total_points}, status=201)


# ==============================================================================
# Rewards API (Reloadly)
# ==============================================================================

def parse_amount_to_decimal(amount_str: str) -> Decimal:
    """
    Parse user-entered amount like '₱50', 'PHP 50.00', '50.00', '1,000.50' → Decimal('...').
    Raises ValueError on invalid input.
    """
    s = (amount_str or "").strip()
    # strip leading currency words/symbols (₱, PHP, Php, P)
    s = re.sub(r"^\s*(?:₱|PHP|Php|php|P)\s*", "", s)
    s = s.replace(",", "")  # allow "1,000.50"
    if not re.fullmatch(r"\d+(\.\d+)?", s):
        raise ValueError("Invalid amount format.")
    return Decimal(s).quantize(TWO_DP, rounding=ROUND_HALF_UP)



class RedeemRewardView(views.APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            user: User = request.user
            raw_phone = (request.data.get("phone_number") or "").strip()
            amount_str = str(request.data.get("amount") or "").strip()

            if not raw_phone or not amount_str:
                return Response({"success": False, "error": "Missing phone or amount."}, status=400)

            try:
                amount_value = parse_amount_to_decimal(amount_str)  # Decimal('50.00')
            except ValueError as e:
                return Response({"success": False, "error": str(e)}, status=400)

            POINTS_PER_PHP = int(getattr(settings, "POINTS_PER_PHP", 10))
            # points are integers; round half up just in case
            points_cost = int((amount_value * POINTS_PER_PHP).to_integral_value(rounding=ROUND_HALF_UP))

            if user.total_points < points_cost:
                return Response(
                    {
                        "success": False,
                        "error": f"Insufficient points. Need {points_cost}, have {user.total_points}.",
                        "needed_points": points_cost,
                        "have_points": user.total_points,
                        "rate_points_per_php": POINTS_PER_PHP,
                    },
                    status=400,
                )

            try:
                phone = normalize_phone(raw_phone, country_code="PH")
            except ReloadlyError as e:
                return Response({"success": False, "error": str(e)}, status=400)

            try:
                op = auto_detect_operator(phone, country_code="PH")
                operator_id = op.get("operatorId")
                if not operator_id:
                    return Response({"success": False, "error": "Could not determine operator."}, status=400)
            except ReloadlyError as e:
                return Response({"success": False, "error": f"Operator detect failed: {e}"}, status=400)
            fixed = op.get("fixedAmounts") or []
            min_amt = op.get("minAmount")
            max_amt = op.get("maxAmount")

            if fixed:
                allowed = {Decimal(str(x)).quantize(TWO_DP) for x in fixed}
                if amount_value not in allowed:
                    return Response(
                        {"success": False, "error": f"Amount must be one of: {[str(a) for a in sorted(allowed)]}"},
                        status=400,
                    )
            else:
                min_dec = Decimal(str(min_amt)).quantize(TWO_DP) if min_amt is not None else None
                max_dec = Decimal(str(max_amt)).quantize(TWO_DP) if max_amt is not None else None
                if (min_dec is not None and amount_value < min_dec) or (max_dec is not None and amount_value > max_dec):
                    bounds_parts = [
                        f"≥ {min_dec}" if min_dec is not None else None,
                        f"≤ {max_dec}" if max_dec is not None else None,
                    ]
                    bounds = " and ".join([p for p in bounds_parts if p])  # filter Nones
                    return Response({"success": False, "error": f"Amount must be {bounds}."}, status=400)
            
            name = (op.get("name") or "").lower()
            if "globe" in name: telco_code = "globe"
            elif "smart" in name or "sun" in name or "tnt" in name: telco_code = "smart"
            elif "dito" in name: telco_code = "dito"
            else: telco_code = "other"
            
            custom_id = f"reward:{user.id}:{timezone.now().strftime('%Y%m%d%H%M%S')}"
            
            rr = RewardRequest.objects.create(
                user=user,
                mobile_number=phone,               # already in 09... format from normalize_phone
                telco=telco_code,
                operator_id=operator_id,
                operator_name=op.get("name") or None,
                points_used=points_cost,
                amount=amount_value,
                status="requested",
                custom_identifier=custom_id, 
            )

            try:
                tx = send_topup(
                    phone=phone,
                    amount=float(amount_value),
                    operator_id=operator_id,
                    custom_identifier=custom_id   # <-- match the saved custom_id
                )
            except ReloadlyError as e:
                rr.status = "rejected"
                rr.date_processed = timezone.now()
                rr.last_error = str(e)           # <-- capture error for audit
                rr.save(update_fields=["status", "date_processed", "last_error"])
                return Response({"success": False, "error": str(e)}, status=400)

            # persist tx info (id + raw payload)
            rr.reloadly_tx_id = str(tx.get("transactionId") or tx.get("id") or "")
            rr.reloadly_raw = tx
            rr.save(update_fields=["reloadly_tx_id", "reloadly_raw"])

            status_up = (tx.get("status") or "").upper()
            if status_up in {"SUCCESS", "SUCCESSFUL", "COMPLETED"}:
                user.total_points -= points_cost
                user.save(update_fields=["total_points"])
                UserPointsLedger.objects.create(
                    user=user,
                    submission=None,
                    source="adjustment",   # or add a new choice like "reward_redeem"
                    delta_points=-points_cost,
                    balance_after=user.total_points,
                    notes=f"Mobile load: {op.get('name') or telco_code} ({phone})",
                )
                rr.status = "paid"
                rr.date_processed = timezone.now()
                rr.processed_by = request.user
                rr.save(update_fields=["status", "date_processed", "processed_by"])
                return Response(
                    {
                        "success": True,
                        "message": "Load sent successfully!",
                        "new_total_points": user.total_points,
                        "amount_php": str(amount_value),
                        "points_cost": points_cost,
                        "rate_points_per_php": POINTS_PER_PHP,
                    },
                    status=201,
                )
            elif status_up in {"PENDING", "PROCESSING", "REQUESTED"}:
                return Response(
                    {
                        "success": True,
                        "message": "Top-up request accepted and processing.",
                        "amount_php": str(amount_value),
                        "points_cost": points_cost,
                        "rate_points_per_php": POINTS_PER_PHP,
                    },
                    status=202,
                )

            else:
                rr.status = "rejected"
                rr.date_processed = timezone.now()
                rr.processed_by = request.user
                rr.save(update_fields=["status", "date_processed", "processed_by"])
                return Response(
                    {"success": False, "error": f"Top-up status: {status_up or 'UNKNOWN'}"},
                    status=400,
                )

        except Exception:
            LOGGER.exception("RedeemReward error")
            return Response({"success": False, "error": "Server error."}, status=500)


@csrf_exempt
def reloadly_webhook(request):
    expected = getattr(settings, "RELOADLY_WEBHOOK_SECRET", "")
    provided = request.headers.get("X-Webhook-Secret") or request.GET.get("secret") or ""
    if expected and str(provided) != str(expected):
        return HttpResponse(status=403)

    try:
        payload = json.loads(request.body.decode("utf-8"))
        txn_status = (payload.get("status") or "").upper()

        # recipientPhone can be a dict or a string
        rp = payload.get("recipientPhone") or payload.get("recipientPhoneNumber")
        if isinstance(rp, dict):
            phone_raw = rp.get("number") or ""
        else:
            phone_raw = rp or ""

        amt_raw = payload.get("requestedAmount") or payload.get("amount") or 0

        try:
            phone_norm = normalize_phone(phone_raw, country_code="PH")
        except Exception:
            phone_norm = phone_raw

        try:
            amount_dec = Decimal(str(amt_raw)).quantize(TWO_DP)
        except Exception:
            return HttpResponse(status=200)

        # try to match by customIdentifier first (if present)
        custom_id = payload.get("customIdentifier") or payload.get("customIdentifierId")
        rr = None
        if custom_id:
            rr = RewardRequest.objects.filter(custom_identifier=custom_id).order_by("-date_requested").first()

        # fallback to phone+amount+requested
        if not rr:
            rr = (
                RewardRequest.objects
                .filter(mobile_number=phone_norm, amount=amount_dec, status="requested")
                .order_by("-date_requested")
                .first()
            )

        if not rr:
            return HttpResponse(status=200)

        # attach webhook payload + tx id
        rr.reloadly_tx_id = str(payload.get("transactionId") or payload.get("id") or rr.reloadly_tx_id or "")
        rr.reloadly_raw = payload

        if txn_status in {"SUCCESS", "SUCCESSFUL", "COMPLETED"}:
            user = rr.user
            # inside webhook, when marking paid:
            if rr.status == "requested":
                user = rr.user
                if user.total_points >= rr.points_used:
                    user.total_points -= rr.points_used
                    user.save(update_fields=["total_points"])
                    UserPointsLedger.objects.create(
                        user=user,
                        submission=None,
                        source="adjustment",
                        delta_points=-rr.points_used,
                        balance_after=user.total_points,
                        notes=f"Mobile load webhook ({phone_norm})",
                    )
                rr.status = "paid"
                rr.date_processed = timezone.now()

        elif txn_status in {"FAILED", "ERROR"}:
            rr.status = "rejected"
            rr.date_processed = timezone.now()

        rr.save(update_fields=["reloadly_tx_id", "reloadly_raw", "status", "date_processed"])
        return HttpResponse(status=200)

    except Exception:
        LOGGER.exception("Reloadly webhook error")
        return HttpResponse(status=400)


# ==============================================================================
# Auth & Staff Approval
# ==============================================================================

# --- Admin: PointsConfig API ---

@require_http_methods(["POST"])
def reauth_admin(request):
    """
    POST {username|email, password} for any Django superuser.
    On success, set a short-lived session flag to allow editing points.
    """
    if _admin_bypass_ok(request):
        request.session["points_edit_ok_until"] = (timezone.now() + timedelta(minutes=5)).isoformat()
        return JsonResponse({"success": True, "until": request.session["points_edit_ok_until"]})

    ident = (request.POST.get("username") or "").strip()   # can be username or email
    password = request.POST.get("password") or ""
    if not ident or not password:
        return JsonResponse({"success": False, "error": "Missing credentials."}, status=400)

    U = get_user_model()
    # Find the account first by username (case-insensitive), then email
    user_obj = (U.objects.filter(**{f"{U.USERNAME_FIELD}__iexact": ident}).first()
                or U.objects.filter(email__iexact=ident).first())

    # Build the auth kwargs using the real USERNAME_FIELD so ModelBackend is happy
    auth_kwargs = {U.USERNAME_FIELD: getattr(user_obj, U.USERNAME_FIELD)} if user_obj else {U.USERNAME_FIELD: ident}

    # Try authenticate with and without request (some custom backends expect request)
    user = authenticate(request, password=password, **auth_kwargs) or authenticate(password=password, **auth_kwargs)

    if not user or not user.is_active or not user.is_superuser:
        return JsonResponse({"success": False, "error": "Invalid admin credentials."}, status=403)

    request.session["points_edit_ok_until"] = (timezone.now() + timedelta(minutes=5)).isoformat()
    return JsonResponse({"success": True, "until": request.session["points_edit_ok_until"]})



class PointsConfigView(views.APIView):
    permission_classes = [IsAdminOnly]
    parser_classes = [JSONParser]

    def get(self, request):
        cfg = PointsConfig.current()
        return Response({
            "success": True,
            "small": cfg.small_bottle_points,
            "large": cfg.large_bottle_points,
            "updated_at": localtime(cfg.updated_at).isoformat() if cfg.updated_at else None,
        })

    def post(self, request):
        try:
            small = int(request.data.get("small", 0))
            large = int(request.data.get("large", 0))
        except Exception:
            return Response({"success": False, "error": "Invalid numbers."}, status=400)
        if small < 0 or large < 0:
            return Response({"success": False, "error": "Values must be ≥ 0."}, status=400)

        cfg = PointsConfig.current()
        cfg.small_bottle_points = small
        cfg.large_bottle_points = large
        cfg.save(update_fields=["small_bottle_points", "large_bottle_points", "updated_at"])
        return Response({"success": True, "small": cfg.small_bottle_points, "large": cfg.large_bottle_points})


class StaffSignupView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        ser = StaffSignupSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        barangay = None
        if ser.validated_data.get("barangay_id"):
            barangay = get_object_or_404(Barangay, pk=ser.validated_data["barangay_id"])

        desired_username = (ser.validated_data.get("username") or "").strip() or ser.validated_data["email"].split("@")[0].lower()
        username = ensure_unique_username(desired_username)

        with transaction.atomic():
            user = User.objects.create(
                username=username,
                first_name=ser.validated_data["first_name"],
                last_name=ser.validated_data["last_name"],
                email=ser.validated_data["email"].lower(),
                mobile_number=ser.validated_data.get("mobile") or None,
                barangay=barangay,
                is_active=False,
                is_approved=False,
                account_status="active",
            )
            user.set_password(ser.validated_data["password"])
            user.save()
            StaffApprovalRequest.objects.create(user=user, requested_barangay=barangay, status="pending")

        return Response({"success": True, "message": "Staff application submitted. Awaiting approval."}, status=201)


class StaffLoginView(views.APIView):
    permission_classes = [permissions.AllowAny]
    def post(self, request):
        ser = StaffLoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        identifier = ser.validated_data["identifier"]
        password = ser.validated_data["password"]

        user = (User.objects.filter(username__iexact=identifier).first()
                or User.objects.filter(email__iexact=identifier).first()
                or User.objects.filter(mobile_number=identifier).first())
        if not user:
            return Response({"success": False, "error": "Account not found."}, status=404)

        # must be true staff
        if not _is_real_staff(user):
            return Response({"success": False, "error": "Account pending approval or not staff."}, status=403)

        if not authenticate(username=user.username, password=password):
            return Response({"success": False, "error": "Incorrect password."}, status=400)

        # do NOT add group here
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"success": True, "token": token.key, "user": {
            "id": user.id, "name": user.get_full_name(), "email": user.email,
            "username": user.username, "mobile": user.mobile_number,
            "barangay": user.barangay.name if user.barangay else None,
        }})



class ApproveStaffView(views.APIView):
    permission_classes = [IsAdminOnly]

    def post(self, request, user_id: int):
        staff = get_object_or_404(User, pk=user_id)
        req = StaffApprovalRequest.objects.filter(user=staff, status="pending").first()

        barangay = req.requested_barangay if (req and req.requested_barangay) else staff.barangay
        if not barangay:
            return Response({"success": False, "error": "Requested barangay is required."}, status=400)

        with transaction.atomic():
            staff.is_approved = True
            staff.is_active = True
            staff.is_staff = True
            staff.save(update_fields=["is_approved", "is_active", "is_staff"])

            staff_group = get_or_create_group("staff")
            staff.groups.add(staff_group)

            ensure_site_and_add_staff(barangay, staff)

            if req:
                req.status = "approved"
                req.decided_by = request.user
                req.decided_at = timezone.now()
                req.save(update_fields=["status", "decided_by", "decided_at"])

        return Response({"success": True})


class RejectStaffView(views.APIView):
    permission_classes = [IsAdminOnly]

    def post(self, request, user_id: int):
        staff = get_object_or_404(User, pk=user_id)
        with transaction.atomic():
            req = StaffApprovalRequest.objects.filter(user=staff, status="pending").first()
            if req:
                req.status = "rejected"
                req.decided_by = request.user
                req.decided_at = timezone.now()
                req.save(update_fields=["status", "decided_by", "decided_at"])

            remove_staff_from_all_sites(staff)
            staff.delete()

        return Response({"success": True})




class ResidentSignupView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        ser = ResidentSignupSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        barangay = None
        if ser.validated_data.get("barangay_id"):
            barangay = get_object_or_404(Barangay, pk=ser.validated_data["barangay_id"])

        desired_username = (ser.validated_data.get("username") or "").strip() or ser.validated_data["email"].split("@")[0].lower()
        username = ensure_unique_username(desired_username)

        with transaction.atomic():
            user = User.objects.create(
                username=username,
                first_name=ser.validated_data["first_name"],
                last_name=ser.validated_data["last_name"],
                email=ser.validated_data["email"].lower(),
                mobile_number=ser.validated_data.get("mobile") or None,
                barangay=barangay,
                is_active=True,
                is_approved=True,
                account_status="active",
            )
            user.set_password(ser.validated_data["password"])
            user.save()
            user.groups.add(get_or_create_group("resident"))

        return Response({"success": True, "message": "Account created."}, status=201)


class ResidentLoginView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        ser = ResidentLoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        identifier = ser.validated_data["identifier"]
        password = ser.validated_data["password"]

        user = (
            User.objects.filter(username__iexact=identifier).first()
            or User.objects.filter(email__iexact=identifier).first()
        )
        if not user:
            return Response({"success": False, "error": "Account not found."}, status=404)

        user_auth = authenticate(username=user.username, password=password)
        if not user_auth:
            return Response({"success": False, "error": "Incorrect password."}, status=400)

        user.groups.add(get_or_create_group("resident"))

        token, _ = Token.objects.get_or_create(user=user)
        return Response({
            "success": True,
            "message": "Login successful.",
            "token": token.key,
            "user": {
                "id": user.id,
                "name": user.get_full_name(),
                "email": user.email,
                "username": user.username,
                "points": user.total_points,
            },
        })
    
class MeView(views.APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        u: User = request.user
        return Response({
            "success": True,
            "user": {
                "id": u.id,
                "username": u.username,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "name": u.get_full_name(),
                "email": u.email,
                "mobile": u.mobile_number,
                "points": u.total_points,
                "barangay": u.barangay.name if u.barangay else None,
            }
        })

    def patch(self, request):
        ser = MeUpdateSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)

        u: User = request.user
        data = ser.validated_data
        updated_fields = []

        def _clean(s: str) -> str:
            # collapse internal whitespace and trim
            return re.sub(r"\s+", " ", (s or "").strip())

        if "first_name" in data:
            u.first_name = _clean(data["first_name"])
            updated_fields.append("first_name")

        if "last_name" in data:
            u.last_name = _clean(data["last_name"])
            updated_fields.append("last_name")

        if "email" in data:
            u.email = (data["email"] or "").lower()
            updated_fields.append("email")

        if "mobile" in data:
            u.mobile_number = (data["mobile"] or None)
            updated_fields.append("mobile_number")

        # Save only what changed (if nothing provided, do nothing)
        if updated_fields:
            u.save(update_fields=updated_fields)

        return Response({
            "success": True,
            "message": "Profile updated.",
            "user": {
                "id": u.id,
                "username": u.username,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "name": u.get_full_name(),  # <- full name after update
                "email": u.email,
                "mobile": u.mobile_number,
                "points": u.total_points,
                "barangay": u.barangay.name if u.barangay else None,
            }
        })


class ChangePasswordView(views.APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser, FormParser, MultiPartParser]  # accept common bodies

    def _first_error_text(self, errors):
        """
        Flatten DRF serializer errors into a single human message.
        Handles:
          {"non_field_errors": ["Old password is incorrect."]}
          {"old_password": ["Old password is incorrect."]}
          {"new_password": ["New password must be at least 8 characters."]}
          {"detail": "Invalid token."}
        """
        if isinstance(errors, dict):
            # common keys first
            for key in ("non_field_errors", "detail", "old_password", "new_password"):
                if key in errors:
                    val = errors[key]
                    if isinstance(val, (list, tuple)) and val:
                        return str(val[0])
                    return str(val)
            # else pick the first field error
            for val in errors.values():
                if isinstance(val, (list, tuple)) and val:
                    return str(val[0])
                if isinstance(val, str):
                    return val
        return "Invalid input."

    def post(self, request):
        ser = ChangePasswordSerializer(data=request.data, context={"request": request})
        if not ser.is_valid():
            return Response({"success": False, "error": self._first_error_text(ser.errors)}, status=400)

        u: User = request.user
        u.set_password(ser.validated_data["new_password"])
        u.save(update_fields=["password"])

        # rotate token so old token can’t be reused
        Token.objects.filter(user=u).delete()
        token = Token.objects.create(user=u)
        return Response({"success": True, "message": "Password changed.", "token": token.key})




# ==============================================================================
# Submissions: intake → QR → claim
# ==============================================================================

class SubmissionIntakeView(views.APIView):
    permission_classes = [IsStaffish]
    parser_classes = [JSONParser]

    def post(self, request):
        ser = SubmissionIntakeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        staff = request.user
        dropoff_site = get_object_or_404(DropOffSite, pk=ser.validated_data["dropoff_site_id"])
        bottle_data = ser.validated_data["bottle_data"]

        proposed_points = compute_points(bottle_data)
        qr_token = secrets.token_urlsafe(24)
        expires = timezone.now() + timedelta(minutes=10)

        with transaction.atomic():
            sub = Submission.objects.create(
                staff=staff,
                dropoff_site=dropoff_site,
                bottle_data=bottle_data,
                proposed_points=proposed_points,
                qr_token=qr_token,
                qr_expires_at=expires,
                status="pending",
                source="manual",
            )
            StaffTransaction.objects.create(staff=staff, submission=sub, action="submission_created")

        return Response(
            {
                "submission_id": sub.id,
                "proposed_points": proposed_points,
                "qr_token": qr_token,
                "qr_expires_at": expires.isoformat(),
            },
            status=201,
        )

@method_decorator(require_staff_json, name='dispatch')
class SubmissionQRView(View):
    """GET /api/submissions/<id>/qr.png → QR PNG of the submission's qr_token"""
    def get(self, request, submission_id: int):
        try:
            sub = Submission.objects.only("qr_token").get(pk=submission_id)
        except Submission.DoesNotExist:
            raise Http404

        try:
            import qrcode  # lazy import
        except ImportError:
            return HttpResponse("QR code generator not installed.", status=500)

        img = qrcode.make(sub.qr_token)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return HttpResponse(buf.read(), content_type="image/png")


class SubmissionClaimView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser]

    def post(self, request):
        ser = SubmissionClaimSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        token = ser.validated_data["qr_token"]

        with transaction.atomic():
            sub = Submission.objects.select_for_update().filter(qr_token=token).first()
            if not sub:
                return Response({"success": False, "error": "Invalid QR."}, status=404)

            if sub.status != "pending":
                return Response({"success": False, "error": "QR already used or invalid state."}, status=400)

            if sub.qr_expires_at and sub.qr_expires_at < timezone.now():
                sub.status = "expired"
                sub.save(update_fields=["status"])
                return Response({"success": False, "error": "QR expired."}, status=400)

            sub.claimed_by = request.user
            sub.claimed_at = timezone.now()
            sub.claimed_points = sub.proposed_points
            sub.status = "claimed"
            sub.save(update_fields=["claimed_by", "claimed_at", "claimed_points", "status", "updated_at"])

            user = request.user
            user.total_points += sub.claimed_points
            user.save(update_fields=["total_points"])

            UserPointsLedger.objects.create(
                user=user,
                submission=sub,
                source="submission_claim",
                delta_points=sub.claimed_points,
                balance_after=user.total_points,
                notes="Claim via QR",
            )

        return Response({"success": True, "claimed_points": sub.claimed_points, "balance": user.total_points})


# ==============================================================================
# Staff & Site metrics / pages
# ==============================================================================

@api_view(["GET"])
@permission_classes([IsStaffish])
def staff_metrics(request, staff_id: int):
    """
    GET /api/staff/metrics/<staff_id>/?range=week|month|year
    Returns totals for the selected period + a breakdown series,
    and also an all-time total for a KPI card.
    """
    # Verify the staff exists (optional but clearer errors)
    staff = get_object_or_404(User, pk=staff_id)

    # period bounds + labels (reuse your helper)
    rng = (request.GET.get("range") or "week").lower()
    start_d, end_d, period_key, labels = _period_bounds(rng)

    # Submissions CREATED by this staff in the window
    qs = Submission.objects.filter(
        staff_id=staff_id,
        created_at__date__gte=start_d,
        created_at__date__lt=end_d,
    ).only("id", "created_at", "bottle_data", "claimed_points")

    # Totals in range
    total_submissions = qs.count()

    # bottles (small+large) in range
    def _sum_bottles(bdata):
        small = large = 0
        for it in bdata or []:
            size = (it.get("size") or "").lower()
            try:
                cnt = int(it.get("count") or it.get("quantity") or 0)
            except Exception:
                cnt = 0
            if size == "small": small += cnt
            elif size == "large": large += cnt
        return small + large

    total_plastic_count = 0
    # Pre-collect dates for series
    if period_key == "year":
        # yyyy-mm buckets
        counts_by_month = {lbl: 0 for lbl in labels}
        for s in qs:
            dt = localtime(s.created_at).date()
            key = f"{dt.year}-{dt.month:02d}"
            if key in counts_by_month:
                counts_by_month[key] += 1
            total_plastic_count += _sum_bottles(s.bottle_data)
        breakdown = [{"label": k, "value": counts_by_month[k]} for k in labels]
    else:
        # daily buckets using "%b %d"
        c = Counter([localtime(s.created_at).date().strftime("%b %d") for s in qs])
        for s in qs:
            total_plastic_count += _sum_bottles(s.bottle_data)
        breakdown = [{"label": lbl, "value": int(c.get(lbl, 0))} for lbl in labels]

    # Lifetime / all-time submissions by this staff
    lifetime_submissions = Submission.objects.filter(staff_id=staff_id).count()

    return Response({
        "success": True,
        "range": period_key,                     # week|month|year
        "total_submissions": total_submissions,  # in-range
        "total_plastic_count": total_plastic_count,  # in-range (pcs)
        "breakdown": breakdown,                  # [{label, value}]
        "lifetime_submissions": lifetime_submissions,  # all-time KPI
        "staff": {
            "id": staff.id,
            "name": staff.get_full_name(),
            "barangay": staff.barangay.name if staff.barangay else None,
        }
    })

from datetime import datetime as _dt

@api_view(["GET"])
@permission_classes([IsStaffish])
def staff_monitor(request, staff_id: int):
    """
    GET /api/staff/monitor/<staff_id>/?from=YYYY-MM-DD&to=YYYY-MM-DD
    Returns:
      {
        "success": true,
        "size_counts": {"small": 0, "large": 0},
        "recent": [
          {
            "id": 123,
            "created_at": "2025-10-18T10:05:00+08:00",
            "resident": "Juan D.",
            "points": 30,
            "status": "claimed",
            "bottles": [{"size":"small","count":2},{"size":"large","count":1}]
          },
          ...
        ]
      }
    """
    # ensure staff exists
    staff = get_object_or_404(User, pk=staff_id)

    # --- bounds (defaults to this week if not provided) ---
    def _parse_date(s: str):
        try:
            return _dt.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return None

    date_from = _parse_date(request.GET.get("from", ""))
    date_to   = _parse_date(request.GET.get("to", ""))

    if not date_from or not date_to or date_from >= date_to:
        # default to Monday..next Monday
        today = today_ph()
        sow = today - timedelta(days=today.weekday())  # Monday
        date_from = sow
        date_to   = sow + timedelta(days=7)

    # --- query submissions CREATED by this staff within window ---
    qs = (
        Submission.objects
        .filter(
            staff_id=staff_id,
            created_at__date__gte=date_from,
            created_at__date__lt=date_to,
        )
        .select_related("claimed_by")
        .only("id","created_at","bottle_data","claimed_points","status","claimed_by__first_name","claimed_by__last_name")
        .order_by("-created_at")
    )

    # --- aggregate small/large ---
    small = large = 0
    def _sum_sizes(bdata):
        s = l = 0
        for it in (bdata or []):
            size = (it.get("size") or "").lower()
            try:
                cnt = int(it.get("count") or it.get("quantity") or 0)
            except Exception:
                cnt = 0
            if size == "small": s += cnt
            elif size == "large": l += cnt
        return s, l

    recent = []
    for sub in qs:
        s, l = _sum_sizes(sub.bottle_data)
        small += s
        large += l
        resident_name = sub.claimed_by.get_full_name() if sub.claimed_by_id else "Resident"
        recent.append({
            "id": sub.id,
            "created_at": localtime(sub.created_at).isoformat(),
            "resident": resident_name or "Resident",
            "points": int(sub.claimed_points or 0),
            "status": sub.status,
            "bottles": [
                {"size": "small", "count": s},
                {"size": "large", "count": l},
            ],
        })

    return Response({
        "success": True,
        "size_counts": {"small": small, "large": large},
        "recent": recent,   # already newest-first
        "from": str(date_from),
        "to": str(date_to),
        "staff": {
            "id": staff.id,
            "name": staff.get_full_name(),
            "barangay": staff.barangay.name if staff.barangay else None,
        }
    })


@require_admin_page
def dropoff_sites_view(request):
    # M2M: prefetch staff_members
    sites = DropOffSite.objects.select_related("barangay").prefetch_related("staff_members")
    return render(request, "dropoff_sites.html", {"sites": sites})

@require_admin_page
@ensure_csrf_cookie
def staff_management_view(request):
    # Only real applications that are waiting for action
    pending_reqs = (
        StaffApprovalRequest.objects
        .select_related("user", "requested_barangay")
        .filter(status="pending")
        .order_by("created_at")
    )

    active_staff = (
        User.objects
        .filter(is_approved=True, is_staff=True, is_superuser=False)
        .order_by("date_joined")
    )

    return render(
        request,
        "staff_management.html",
        {"pending_reqs": pending_reqs, "active_staff": active_staff},
    )
    
@require_admin_page
@ensure_csrf_cookie
def submissions_admin_view(request):
    """
    Server-rendered admin page that shows:
      - Editable PointsConfig (small/large)
      - Sites list (with assigned staff), links to site detail + CSV export buttons
    POST updates the PointsConfig.
    """
    cfg = PointsConfig.current()
    if request.method == "POST":
        # Must have a valid re-auth window
        until_iso = request.session.get("points_edit_ok_until")
        can_edit = False
        if until_iso:
            try:
                until_dt = timezone.make_aware(datetime.fromisoformat(until_iso)) if "Z" not in until_iso else datetime.fromisoformat(until_iso)
            except Exception:
                until_dt = None
            if until_dt and until_dt > timezone.now():
                can_edit = True

        if not can_edit:
            # refuse the update silently (or add a message)
            sites = (
                DropOffSite.objects.select_related("barangay")
                .prefetch_related("staff_members").order_by("barangay__name")
            )
            return render(
                request,
                "submissions_admin.html",
                {
                    "cfg": cfg,
                    "sites": sites,
                    "points_edit_error": "Re-auth as superuser required before editing.",
                },
            )

        # proceed with saving (already your code)
        try:
            small = int(request.POST.get("small", cfg.small_bottle_points))
            large = int(request.POST.get("large", cfg.large_bottle_points))
        except Exception:
            small = cfg.small_bottle_points
            large = cfg.large_bottle_points

        if small >= 0 and large >= 0:
            cfg.small_bottle_points = small
            cfg.large_bottle_points = large
            cfg.save(update_fields=["small_bottle_points", "large_bottle_points", "updated_at"])

    sites = (
        DropOffSite.objects
        .select_related("barangay")
        .prefetch_related("staff_members")
        .order_by("barangay__name")
    )

    return render(
        request,
        "submissions_admin.html",  # you'll create this template
        {
            "cfg": cfg,
            "sites": sites,
        },
    )



@require_admin_page
def submissions_by_dropoff_site(request, site_id: int):
    site = get_object_or_404(DropOffSite, id=site_id)
    submissions = Submission.objects.filter(dropoff_site=site).select_related("staff", "claimed_by").order_by("-created_at")
    return render(request, "submissions_by_site.html", {"site": site, "submissions": submissions})



@api_view(["GET"])
@permission_classes([IsStaffish])
def staff_transaction_history(request, staff_id: int):
    """
    GET /api/staff/<staff_id>/transactions/
    Returns newest-first history with submission details + QR URL.
    """
    # verify staff exists
    staff = get_object_or_404(User, pk=staff_id)

    # if you configured ADMIN_SHARED_KEY, include it so the mobile app can open the PNG
    admin_key = getattr(settings, "ADMIN_SHARED_KEY", None)

    txs = (
        StaffTransaction.objects
        .filter(staff_id=staff_id)
        .select_related("submission")
        .order_by("-created_at")
    )

    items = []
    for t in txs:
        sub = t.submission
        sub_dict = None
        if sub:
            qr_url = request.build_absolute_uri(
                reverse("submission_qr", args=[sub.id])

            )
            # so staff mobile can fetch the PNG without cookie/session
            if admin_key:
                join = "&" if "?" in qr_url else "?"
                qr_url = f"{qr_url}{join}admin_key={admin_key}"

            sub_dict = {
                "id": sub.id,
                "status": sub.status,  # pending | claimed | expired
                "created_at": localtime(sub.created_at).isoformat(),
                "claimed_at": (localtime(sub.claimed_at).isoformat() if sub.claimed_at else None),
                "qr_expires_at": (localtime(sub.qr_expires_at).isoformat() if sub.qr_expires_at else None),
                "claimed_points": int(sub.claimed_points or 0),
                "proposed_points": int(sub.proposed_points or 0),
                "bottle_data": sub.bottle_data or [],
                "qr_url": qr_url,  # PNG
            }

        items.append({
            "action": t.action,                # e.g. "submission_created"
            "notes": t.notes or "",
            "points": int(getattr(sub, "claimed_points", 0) or 0),
            "date": timezone.localtime(t.created_at).strftime("%Y-%m-%d %H:%M"),
            "submission": sub_dict,
        })

    return Response({"success": True, "transactions": items})



def _period_bounds(param: str) -> Tuple[date, date, str, List[str]]:
    """
    Returns (start_date, end_date_exclusive, period_key, label_list) for site detail chart.
    """
    today = today_ph()
    if param == "year":
        # last 12 months (inclusive of current month)
        labels = []
        months = []
        y, m = today.year, today.month
        for i in range(11, -1, -1):
            yy = y if m - i > 0 else y - 1
            mm = ((m - i - 1) % 12) + 1
            months.append((yy, mm))
            labels.append(f"{yy}-{mm:02d}")
        start = date(months[0][0], months[0][1], 1)
        # exclusive end: first day of next month after last
        last_y, last_m = months[-1]
        if last_m == 12:
            end = date(last_y + 1, 1, 1)
        else:
            end = date(last_y, last_m + 1, 1)
        return start, end, "year", labels

    if param == "month":
        # last 30 days
        start = today - timedelta(days=29)
        end = today + timedelta(days=1)
        labels = [(start + timedelta(days=i)).strftime("%b %d") for i in range(30)]
        return start, end, "month", labels

    # default week
    start = today - timedelta(days=6)
    end = today + timedelta(days=1)
    labels = [(start + timedelta(days=i)).strftime("%b %d") for i in range(7)]
    return start, end, "week", labels

@require_admin_page
def dropoff_site_detail(request, site_id: int):
    site = get_object_or_404(DropOffSite, id=site_id)
    site_staff = list(site.staff_members.all())

    # Period filter for charts
    period = (request.GET.get("period") or "week").lower()
    start_d, end_d, period_key, labels = _period_bounds(period)

    # Base queryset WITHOUT select_related; use it for aggregates/light reads
    base_qs = (
        Submission.objects
        .filter(
            dropoff_site=site,
            created_at__date__gte=start_d,
            created_at__date__lt=end_d
        )
        .order_by("-created_at")
    )

    # Table queryset WITH select_related; no .only() calls here
    submissions = base_qs.select_related("staff", "claimed_by")

    # Totals (over selected period)
    total_submissions = base_qs.count()
    total_points = base_qs.aggregate(total=Sum("claimed_points"))["total"] or 0

    # Bottle distribution (read just the JSON field, no select_related needed)
    from collections import Counter
    counter = Counter()
    total_bottles = 0
    for bdata in base_qs.values_list("bottle_data", flat=True):
        for b in (bdata or []):
            size = (b.get("size") or "unknown").lower()
            try:
                cnt = int(b.get("count") or b.get("quantity") or 0)
            except Exception:
                cnt = 0
            counter[size] += cnt
            total_bottles += cnt

    bottle_labels = list(counter.keys())
    bottle_values = list(counter.values())

    # Time-series counts for chart
    series_labels = labels
    if period_key == "year":
        counts_by_month = {lbl: 0 for lbl in labels}
        for dt in base_qs.values_list("created_at", flat=True):
            d = localtime(dt).date()
            key = f"{d.year}-{d.month:02d}"
            if key in counts_by_month:
                counts_by_month[key] += 1
        series_values = [counts_by_month[k] for k in labels]
    else:
        from collections import Counter as C2
        day_keys = [
            localtime(dt).date().strftime("%b %d")
            for dt in base_qs.values_list("created_at", flat=True)
        ]
        c = C2(day_keys)
        series_values = [int(c.get(lbl, 0)) for lbl in labels]

    return render(
        request,
        "dropoff_site_detail.html",
        {
            "site": site,
            "staff_members": site_staff,
            "period": period_key,
            "submissions": submissions,  # the table uses this
            "total_submissions": total_submissions,
            "total_points": total_points,
            "total_bottles": total_bottles,
            "series_labels": json.dumps(series_labels),
            "series_values": json.dumps(series_values),
            "bottle_labels": json.dumps(bottle_labels),
            "bottle_values": json.dumps(bottle_values),
        },
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication, BasicAuthentication])
@permission_classes([permissions.IsAuthenticated])
def get_user_details(request, user_id: int):
    user = User.objects.filter(pk=user_id).first()
    if not user:
        return Response({"success": False, "error": "User not found."}, status=404)

    # allow the user themselves, or staff-ish, or admin-bypass
    if not (
        request.user.id == user.id
        or _admin_bypass_ok(request)
        or request.user.is_superuser
        or request.user.is_staff
        or request.user.groups.filter(name="staff").exists()
    ):
        return Response({"success": False, "error": "Forbidden."}, status=403)

    total_points = user.total_points or 0
    WEEKLY_TARGET = 50
    sow = today_ph() - timedelta(days=today_ph().weekday())
    weekly = Submission.objects.filter(claimed_by=user, created_at__date__gte=sow)

    bottles_this_week = 0
    for sub in weekly.only("bottle_data"):
        for b in sub.bottle_data or []:
            bottles_this_week += int(b.get("count") or 0)

    quota_progress = min(bottles_this_week / WEEKLY_TARGET, 1.0) if WEEKLY_TARGET > 0 else 0.0
    return Response({
        "success": True,
        "id": user.id,
        "name": user.get_full_name(),
        "points": total_points,
        "quota_progress": quota_progress
    })


# ==============================================================================
# Image analysis (prototype)
# ==============================================================================

@csrf_exempt
@api_view(["POST"])
@parser_classes([MultiPartParser])
def analyze_image(request):
    image_file = request.FILES.get("image")
    if not image_file:
        return JsonResponse({"error": "No image provided"}, status=400)

    file_bytes = np.asarray(bytearray(image_file.read()), dtype=np.uint8)
    img = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
    if img is None:
        return JsonResponse({"error": "Failed to decode image"}, status=400)

    img = cv2.resize(img, (640, 480))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.medianBlur(gray, 5)
    edges = cv2.Canny(blurred, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    bottle_counts = {"small": 0, "large": 0}
    for contour in contours:
        _, _, w, h = cv2.boundingRect(contour)
        if h < 100 or w < 30:
            continue
        if h <= 200:
            bottle_counts["small"] += 1
        else:
            bottle_counts["large"] += 1

    total_detected = sum(bottle_counts.values())
    confidence = round(min(1.0, total_detected / 5.0), 2)
    suggested_size = max(bottle_counts, key=bottle_counts.get) if total_detected > 0 else "unknown"

    return JsonResponse(
        {
            "total_detected": total_detected,
            "confidence_score": confidence,
            "suggested_size": suggested_size,
            "bottle_sizes": bottle_counts,
        }
    )


# ==============================================================================
# CSV Exports (NOW: SUMMARY CSVs)
# ==============================================================================

def _summarize_bottles(bottle_data):
    small = large = 0
    for it in bottle_data or []:
        size = (it.get("size") or "").lower()
        try:
            count = int(it.get("count", it.get("quantity", 0)) or 0)
        except Exception:
            count = 0
        if size == "small":
            small += count
        elif size == "large":
            large += count
    return {"small": small, "large": large}


def _apply_submission_filters(qs, request):
    """
    Optional filters: ?from=YYYY-MM-DD&to=YYYY-MM-DD&status=claimed|pending|...
    """
    date_from = request.GET.get("from")
    date_to = request.GET.get("to")
    status = request.GET.get("status")
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    if status:
        qs = qs.filter(status=status)
    return qs


def _months_between(d1: date, d2: date) -> int:
    """Inclusive month span, min 1. If d1 > d2 returns 1."""
    if not d1 or not d2 or d1 > d2:
        return 1
    return (d2.year - d1.year) * 12 + (d2.month - d1.month) + 1


def _site_summary(site: DropOffSite, base_qs) -> dict:
    """
    Compute summary metrics for a site from a base submissions queryset already filtered by site & date range.
    Returns dict with totals and averages.
    """
    qs = base_qs

    # basic aggregations
    agg = qs.aggregate(
        total_claimed_points=Sum("claimed_points"),
        first_dt=Min("created_at"),
        last_dt=Max("created_at"),
    )
    total_submissions = qs.count()
    total_points = int(agg["total_claimed_points"] or 0)

    # bottle totals (iterate light)
    small = large = 0
    for s in qs.only("bottle_data"):
        bsum = _summarize_bottles(s.bottle_data)
        small += bsum["small"]
        large += bsum["large"]
    total_bottles = small + large

    first_date = localtime(agg["first_dt"]).date() if agg["first_dt"] else None
    last_date = localtime(agg["last_dt"]).date() if agg["last_dt"] else None
    months_covered = _months_between(first_date, last_date) if first_date and last_date else 1

    avg_submissions_per_month = round(total_submissions / months_covered, 2) if total_submissions else 0.0
    avg_points_per_submission = round(total_points / total_submissions, 2) if total_submissions else 0.0
    avg_bottles_per_submission = round(total_bottles / total_submissions, 2) if total_submissions else 0.0

    staff_count = site.staff_members.count()
    residents_served = (
        qs.exclude(claimed_by__isnull=True).values_list("claimed_by_id", flat=True).distinct().count()
    )

    return {
        "site_id": site.id,
        "barangay": site.barangay.name if site.barangay else "",
        "staff_count": staff_count,
        "total_submissions": total_submissions,
        "total_bottles_small": small,
        "total_bottles_large": large,
        "total_bottles": total_bottles,
        "total_claimed_points": total_points,
        "first_submission_date": first_date.isoformat() if first_date else "",
        "last_submission_date": last_date.isoformat() if last_date else "",
        "months_covered": months_covered,
        "avg_submissions_per_month": avg_submissions_per_month,
        "avg_points_per_submission": avg_points_per_submission,
        "avg_bottles_per_submission": avg_bottles_per_submission,
        "unique_residents_served": residents_served,
    }


def _summary_csv_response(filename: str, rows: List[dict]) -> HttpResponse:
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response.write("\ufeff")  # BOM for Excel
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    headers = [
        "site_id",
        "barangay",
        "staff_count",
        "total_submissions",
        "total_bottles_small",
        "total_bottles_large",
        "total_bottles",
        "total_claimed_points",
        "first_submission_date",
        "last_submission_date",
        "months_covered",
        "avg_submissions_per_month",
        "avg_points_per_submission",
        "avg_bottles_per_submission",
        "unique_residents_served",
    ]

    writer = csv.DictWriter(response, fieldnames=headers)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in headers})
    return response

@require_admin_page
def export_all_submissions_csv(request):
    rows = []
    sites = DropOffSite.objects.select_related("barangay").prefetch_related("staff_members")
    for site in sites:
        qs = Submission.objects.filter(dropoff_site=site)
        qs = _apply_submission_filters(qs, request)
        rows.append(_site_summary(site, qs))
    return _summary_csv_response("sites_summary_all.csv", rows)

@require_admin_page
def export_site_submissions_csv(request, site_id: int):
    site = get_object_or_404(
        DropOffSite.objects.select_related("barangay").prefetch_related("staff_members"),
        pk=site_id
    )
    qs = Submission.objects.filter(dropoff_site=site)
    qs = _apply_submission_filters(qs, request)
    row = _site_summary(site, qs)
    return _summary_csv_response(f"site_{site.id}_summary.csv", [row])

# ==============================================================================
# Drop-off Site deletion (used by template's JS button)
# ==============================================================================

@require_POST
@require_admin_json
def delete_dropoff_site(request, site_id: int):
    site = get_object_or_404(DropOffSite, pk=site_id)
    site.delete()
    return JsonResponse({"success": True})