# views.py — Recyclean (auth, submissions, rewards, DIY CRUD + daily rotation + summary CSVs)
from __future__ import annotations

import csv
import io
import json
import logging
import secrets
import requests
from django.contrib import messages
import re
from datetime import datetime, timedelta, date
from functools import wraps
from typing import List, Dict, Tuple
from collections import Counter
from ultralytics import YOLO
import torch
from threading import Lock
from PIL import Image
import numpy as np
from django.urls import reverse
from django.conf import settings
from django.contrib.auth import authenticate, logout
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
from django.utils.dateparse import parse_datetime
from calendar import monthrange

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
    DetectionSession,
)

from .services.reloadly import (
    get_reloadly_balance,
    list_reloadly_transactions,
    send_topup,
    auto_detect_operator,
    normalize_phone,
    ReloadlyError,
)

# add near your imports (top of file)
from django.core.signing import TimestampSigner, BadSignature, SignatureExpired
signer = TimestampSigner(salt="submission-qr")

# YouTube metadata (optional dependency)
try:
    from pytube import YouTube  # type: ignore[import-not-found]
except ImportError:
    YouTube = None  # type: ignore[assignment]




LOGGER = logging.getLogger(__name__)
COUNTRY = getattr(settings, "RELOADLY_COUNTRY_CODE", "PH")
TWO_DP = Decimal("0.01")

# ==============================================================================
# Utilities
# ==============================================================================

# ---------- YOLO: lazy singleton ----------
_YOLO_MODEL = None
_YOLO_LOCK = Lock()   # protect first load / shared model use

def _get_yolo():
    from django.conf import settings as _s
    global _YOLO_MODEL
    with _YOLO_LOCK:
        if _YOLO_MODEL is None:
            _YOLO_MODEL = YOLO(str(_s.YOLO_SEG_WEIGHTS))
            _YOLO_MODEL.to(_s.YOLO_DEVICE)
            # light warm-up to compile kernels / JIT paths
            try:
                with torch.inference_mode():
                    _YOLO_MODEL.predict(
                        source=np.zeros(( _s.YOLO_IMG_SIZE, _s.YOLO_IMG_SIZE, 3), dtype=np.uint8),
                        conf=_s.YOLO_CONF, iou=_s.YOLO_IOU, imgsz=_s.YOLO_IMG_SIZE, device=_s.YOLO_DEVICE,
                        verbose=False, max_det=1
                    )
            except Exception:
                pass
    return _YOLO_MODEL

_PREDICT_LOCK = Lock()

def _run_yolo_and_summarize(file_obj):
    from django.conf import settings as _s
    model = _get_yolo()

    # Open once; keep PIL image for Ultralytics
    img = Image.open(file_obj).convert("RGB")
    w, h = img.size

    with _PREDICT_LOCK, torch.inference_mode():
        r = model.predict(
            source=img,
            conf=_s.YOLO_CONF,
            iou=_s.YOLO_IOU,
            imgsz=_s.YOLO_IMG_SIZE,
            device=_s.YOLO_DEVICE,
            verbose=False,
            max_det=_s.YOLO_MAX_DET,
        )[0]

    names = list(getattr(_s, "SEG_CLASS_NAMES", []))  # e.g., ["small_bottle", "large_bottle"]

    items = []
    counts = {"small": 0, "large": 0}

    has_masks = getattr(r, "masks", None) is not None and getattr(r.masks, "data", None) is not None
    mask_area_total = None
    if has_masks:
        mh, mw = r.masks.data.shape[-2], r.masks.data.shape[-1]
        mask_area_total = float(mh * mw) if mh and mw else None

    n = len(r.boxes)
    for i in range(n):
        cls_id = int(r.boxes.cls[i].item())
        conf   = float(r.boxes.conf[i].item())
        name   = names[cls_id] if 0 <= cls_id < len(names) else f"class_{cls_id}"
        xyxy   = r.boxes.xyxy[i].tolist() if getattr(r.boxes, "xyxy", None) is not None else None

        area_frac = None
        if has_masks and mask_area_total:
            m = r.masks.data[i]
            area_frac = float(m.sum().item()) / mask_area_total if mask_area_total > 0 else None

        # map class name → "small" / "large"
        size_key = "small" if "small" in name else ("large" if "large" in name else None)
        if size_key in counts:
            counts[size_key] += 1

        items.append({
            "cls_id": cls_id,
            "cls_name": name,
            "confidence": conf,
            "bbox_xyxy": xyxy,
            "area_frac": area_frac,
        })

    return items, counts, w, h



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

class IsAdminOrStaff(BasePermission):
    def has_permission(self, request, view):
        # allow shared-key/dev bypass too
        if _admin_bypass_ok(request):
            return True
        return _is_admin_only(request.user) or _is_real_staff(request.user)


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

class IsResident(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(
            u and u.is_authenticated and u.is_active
            and not u.is_staff                      # hard-stop staff
            and u.groups.filter(name="resident").exists()
        )

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
    return render(
        request,
        "diy_dashboard.html",
        {
            "form": form,
            "tutorials": tutorials,
            "DIY_PERIOD": getattr(settings, "DIY_PERIOD", "week"),
            "DIY_DAILY_COUNT": int(getattr(settings, "DIY_DAILY_COUNT", 3)),
        },
    )


def _abs_or_none(request, f):
    return request.build_absolute_uri(f.url) if f else None


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
@authentication_classes([TokenAuthentication, SessionAuthentication, BasicAuthentication])
def api_diy_daily(request):
    """
    Weekly mode:
      - Uses the week's anchor date (DIY_WEEK_START) as the storage key in DIYDailySelection/Pool.date
      - Picks DIY_DAILY_COUNT tutorials per WEEK (default 3)
      - Enforces DIY_NO_REPEAT_WEEKS across anchors
    Daily mode (back-compat):
      - Behaves as before (count per day; DIY_NO_REPEAT_DAYS)
    Per-user:
      - Excludes tutorials the user submitted within the cooldown window from their view.
    """
    today = today_ph()
    anchor = _period_anchor(today)

    desired_count = int(request.GET.get("count") or getattr(settings, "DIY_DAILY_COUNT", 3))
    refresh = str(request.GET.get("refresh", "0")).lower() in {"1", "true", "yes"}

    # If refreshing, clear current period's selections (by anchor)
    if refresh:
        DIYDailySelection.objects.filter(date=anchor).delete()

    existing = (
        DIYDailySelection.objects
        .filter(date=anchor)
        .select_related("tutorial")
        .order_by("id")
    )

    # --- per-user recent submission ids (for hiding + flag) ---
    user = request.user if request.user.is_authenticated else None
    recent_user_tids = set()
    if user:
        cutoff = _cooldown_cutoff()
        recent_user_tids = set(
            DIYSubmission.objects
            .filter(user=user, created_at__date__gte=cutoff)
            .values_list("tutorial_id", flat=True)
            .distinct()
        )

    # Short-circuit if we already have this week's picks stored
    if not refresh and existing.count() >= desired_count:
        tutorials = [e.tutorial for e in existing[:desired_count]]

        # HIDE tutorials the user has recently submitted (cooldown)
        if user and recent_user_tids:
            tutorials = [t for t in tutorials if t.id not in recent_user_tids]

        # Build has_submitted map only within cooldown window
        has_recent = {}
        if user and tutorials:
            shown_ids = [t.id for t in tutorials]
            has_recent = {tid: (tid in recent_user_tids) for tid in shown_ids}

        return Response({
            "date": str(anchor),
            "tutorials": [
                {
                    "id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "description_html": _render_bullets_or_paragraph(t.description),
                    "video_url": t.video_url,
                    "thumbnail": _abs_or_none(request, t.thumbnail) or _youtube_thumb(t.video_url),
                    "points_on_submit": t.points_on_submit,
                    # True only if still in cooldown
                    "has_submitted": (has_recent.get(t.id, False) if user else False),
                }
                for t in tutorials
            ],
        })

    # Build/sync the pool for this anchor
    pool, _ = DIYDailyPool.objects.get_or_create(date=anchor)

    # keep pool in sync with all active tutorials
    active_ids = set(DIYTutorial.objects.filter(is_active=True).values_list("id", flat=True))
    pool_ids = set(pool.tutorials.values_list("id", flat=True))
    to_add = active_ids - pool_ids
    if to_add:
        pool.tutorials.add(*to_add)
    to_remove = pool_ids - active_ids
    if to_remove:
        pool.tutorials.remove(*to_remove)

    # No-repeat window across recent periods
    window_start = _recent_window_start(anchor)
    recent_ids = set(
        DIYDailySelection.objects
        .filter(date__gte=window_start)
        .values_list("tutorial_id", flat=True)
        .distinct()
    )

    # Candidate universe for THIS USER this week:
    base_qs = pool.tutorials.filter(is_active=True)

    candidates_excl = list(base_qs.exclude(id__in=recent_ids).order_by("id").values_list("id", flat=True))
    candidates_all  = list(base_qs.order_by("id").values_list("id", flat=True))

    ids = candidates_excl if candidates_excl else candidates_all
    if not ids:
        # nothing left for this user this week
        return Response(status=204)

    # Round-robin start = function of the *anchor* (weekly) + salt
    salt = int(getattr(settings, "DIY_ROTATION_SALT", 0))
    start_idx = (anchor.toordinal() + salt) % len(ids)

    picked_ids_now = set(existing.values_list("tutorial_id", flat=True))
    picked = []
    i = 0
    while len(picked) < desired_count and i < len(ids) * 2:
        tid = ids[(start_idx + i) % len(ids)]
        if tid not in picked and tid not in picked_ids_now:
            picked.append(tid)
        i += 1

    if len(picked) < desired_count:
        for tid in candidates_all:
            if len(picked) >= desired_count:
                break
            if tid not in picked and tid not in picked_ids_now:
                picked.append(tid)

    for tid in picked:
        DIYDailySelection.objects.get_or_create(
            date=anchor,
            tutorial_id=tid,
            defaults={"pool": pool},
        )

    final = (
        DIYDailySelection.objects
        .filter(date=anchor)
        .select_related("tutorial")
        .order_by("id")
    )[:desired_count]

    tutorials = [e.tutorial for e in final]

    # HIDE cooldown tutorials from the response
    if user and recent_user_tids:
        tutorials = [t for t in tutorials if t.id not in recent_user_tids]

    # has_submitted map (within cooldown only)
    has_recent = {}
    if user and tutorials:
        shown_ids = [t.id for t in tutorials]
        has_recent = {tid: (tid in recent_user_tids) for tid in shown_ids}

    return Response({
        "date": str(anchor),
        "tutorials": [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "description_html": _render_bullets_or_paragraph(t.description),
                "video_url": t.video_url,
                "thumbnail": _abs_or_none(request, t.thumbnail) or _youtube_thumb(t.video_url),
                "points_on_submit": t.points_on_submit,
                "has_submitted": (has_recent.get(t.id, False) if user else False),
            }
            for t in tutorials
        ],
    })


# --- helpers near your other utils ---

# --- DIY weekly helpers ---

# --- DIY per-user cooldown helpers ---
def _cooldown_days() -> int:
    return int(getattr(settings, "DIY_USER_COOLDOWN_DAYS", 90))

def _cooldown_cutoff():
    return today_ph() - timedelta(days=_cooldown_days())

def _week_anchor(d: date) -> date:
    """Return the start-of-week date using DIY_WEEK_START (0=Mon..6=Sun)."""
    wk_start = int(getattr(settings, "DIY_WEEK_START", 0))
    offset = (d.weekday() - wk_start) % 7
    return d - timedelta(days=offset)

def _period_anchor(d: date) -> date:
    """Return anchor date based on DIY_PERIOD (day|week)."""
    period = (getattr(settings, "DIY_PERIOD", "day") or "day").lower()
    return _week_anchor(d) if period == "week" else d

def _recent_window_start(anchor: date) -> date:
    """
    Return start date for the no-repeat window depending on DIY_PERIOD.
    For weekly: use DIY_NO_REPEAT_WEEKS (count of weeks, inclusive).
    For daily : use DIY_NO_REPEAT_DAYS  (count of days, inclusive).
    """
    period = (getattr(settings, "DIY_PERIOD", "day") or "day").lower()
    if period == "week":
        n_weeks = max(int(getattr(settings, "DIY_NO_REPEAT_WEEKS", 4)), 1)
        # include current anchor → look back (n_weeks-1) full weeks
        return anchor - timedelta(weeks=n_weeks - 1)
    else:
        n_days = max(int(getattr(settings, "DIY_NO_REPEAT_DAYS", 5)), 1)
        return anchor - timedelta(days=n_days - 1)

# Re-seed a specific date after a featured tutorial was removed/deleted.
def _reseed_for_date(date_val, request=None, desired_count=None):
    if desired_count is None:
        desired_count = int(getattr(settings, "DIY_DAILY_COUNT", 3))

    anchor = _period_anchor(date_val)

    pool, _ = DIYDailyPool.objects.get_or_create(date=anchor)

    # sync pool with current active tutorials
    active_ids = set(DIYTutorial.objects.filter(is_active=True).values_list("id", flat=True))
    pool_ids = set(pool.tutorials.values_list("id", flat=True))
    to_add = active_ids - pool_ids
    to_remove = pool_ids - active_ids
    if to_add:
        pool.tutorials.add(*to_add)
    if to_remove:
        pool.tutorials.remove(*to_remove)

    existing = (
        DIYDailySelection.objects
        .filter(date=anchor)
        .select_related("tutorial")
        .order_by("id")
    )
    if existing.count() >= desired_count:
        return

    window_start = _recent_window_start(anchor)
    recent_ids = set(
        DIYDailySelection.objects
        .filter(date__gte=window_start)
        .values_list("tutorial_id", flat=True)
        .distinct()
    )

    base_qs = pool.tutorials.filter(is_active=True)
    candidates_excl = list(base_qs.exclude(id__in=recent_ids).order_by("id").values_list("id", flat=True))
    candidates_all  = list(base_qs.order_by("id").values_list("id", flat=True))
    ids = candidates_excl if candidates_excl else candidates_all
    if not ids:
        return

    salt = int(getattr(settings, "DIY_ROTATION_SALT", 0))
    start_idx = (anchor.toordinal() + salt) % len(ids)

    picked_ids_now = set(existing.values_list("tutorial_id", flat=True))
    picked = []
    i = 0
    while len(picked) < desired_count and i < len(ids) * 2:
        tid = ids[(start_idx + i) % len(ids)]
        if tid not in picked and tid not in picked_ids_now:
            picked.append(tid)
        i += 1

    if len(picked) < desired_count:
        for tid in candidates_all:
            if len(picked) >= desired_count:
                break
            if tid not in picked and tid not in picked_ids_now:
                picked.append(tid)

    for tid in picked:
        DIYDailySelection.objects.get_or_create(
            date=anchor,
            tutorial_id=tid,
            defaults={"pool": pool},
        )

def _youtube_oembed_author(url: str) -> tuple[str | None, str | None]:
    """
    Best-effort fetch of channel/author from YouTube oEmbed.
    Returns (author_name, author_url) or (None, None).
    """
    if not url:
        return None, None
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=6,
        )
        if r.ok:
            data = r.json()
            return (data.get("author_name") or None, data.get("author_url") or None)
    except Exception:
        pass
    return None, None


def _youtube_meta_safe(url: str):
    """
    Returns (title, duration_seconds, thumbnail_url, description) or (None, None, None, None).
    1) Try pytube for title/length/thumb/description.
    2) Fallback to YouTube oEmbed (title + thumbnail only).
    """
    if not url:
        return None, None, None, None

    # 1) pytube
    if YouTube is not None:
        try:
            yt = YouTube(url)
            title = yt.title or None
            length = int(getattr(yt, "length", 0) or 0) or None
            thumb  = yt.thumbnail_url or None
            desc   = getattr(yt, "description", None) or None
            if not thumb:
                vid = _youtube_id(url or "")
                if vid:
                    thumb = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
            if title or length or thumb or desc:
                return title, length, thumb, desc
        except Exception:
            pass  # fall through

    # 2) oEmbed (no API key): title + thumbnail
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=6
        )
        if r.ok:
            data = r.json()
            title = data.get("title") or None
            thumb = data.get("thumbnail_url") or _youtube_thumb(url)
            return title, None, thumb, None
    except Exception:
        pass

    # Last ditch: derive thumbnail from ID
    return None, None, _youtube_thumb(url), None



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


@api_view(["GET"])
@permission_classes([IsAdminOrStaff])  # admin/staff; adjust if you want
def youtube_meta(request):
    """
    GET /api/utils/youtube-meta/?url=...
    -> {success, title, duration_seconds, thumbnail, description, author_name, author_url}
    """
    url = (request.GET.get("url") or "").strip()
    if not url:
        return Response({"success": False, "error": "Missing url."}, status=400)

    # Keep your current robust meta fetch
    title, dur, thumb, desc = _youtube_meta_safe(url)

    # NEW: add author/channel info from oEmbed (cheap & keyless)
    author_name, author_url = _youtube_oembed_author(url)

    if not (title or dur or thumb):
        return Response({"success": False, "error": "Could not fetch metadata."}, status=400)

    return Response({
        "success": True,
        "title": title,
        "duration_seconds": dur,
        "thumbnail": thumb,
        "description": desc,
        "author_name": author_name,
        "author_url": author_url,
    })



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

        # NEW: snap to period anchor (weekly → week start)
        anchor = _period_anchor(picked_date)

        tutorial = get_object_or_404(DIYTutorial, pk=tutorial_id)
        pool, _ = DIYDailyPool.objects.get_or_create(date=anchor)
        pool.tutorials.add(tutorial)

        DIYDailySelection.objects.get_or_create(
            date=anchor,
            tutorial=tutorial,
            defaults={"pool": pool},
        )
        return JsonResponse({"success": True})
    except Exception as e:
        LOGGER.exception("Feature DIY error")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# views.py
@require_POST
@require_admin_json
def diy_create_tutorial(request):
    form = DIYTutorialForm(request.POST, request.FILES)
    if form.is_valid():
        t = form.save(commit=False)

        force_refresh = bool(form.cleaned_data.get("force_refresh_meta"))
        # On create there is no old url; treat as changed
        url_changed = True

        # Autofill if we should or if fields are blank
        need_meta = (
            t.video_url and (
                force_refresh or url_changed or
                not (t.title or "").strip() or
                not t.duration_seconds or
                not (t.description or "").strip()
            )
        )

        if need_meta:
            meta_title, meta_len, _thumb, meta_desc = _youtube_meta_safe(t.video_url)
            if (not (t.title or "").strip()) and meta_title:
                t.title = meta_title
            if (not t.duration_seconds) and meta_len:
                t.duration_seconds = meta_len
            if (not (t.description or "").strip()):
                if meta_desc:
                    t.description = meta_desc[:4000]
                else:
                    a_name, a_url = _youtube_oembed_author(t.video_url)
                    if a_url:
                        who = a_name or "this creator"
                        t.description = f"Subscribe to: {who} — {a_url}"

        t.save()
        return JsonResponse({"success": True, "id": t.id})
    return JsonResponse({"success": False, "errors": form.errors}, status=400)



@require_POST
@require_admin_json
def diy_update_tutorial(request, tutorial_id: int):
    t = get_object_or_404(DIYTutorial, pk=tutorial_id)
    old_url = t.video_url or ""
    form = DIYTutorialForm(request.POST, request.FILES, instance=t)
    if not form.is_valid():
        return JsonResponse({"success": False, "errors": form.errors}, status=400)

    t = form.save(commit=False)

    url_changed = (old_url.strip() != (t.video_url or "").strip())
    # read from cleaned_data
    force_refresh = bool(form.cleaned_data.get("force_refresh_meta"))

    if t.video_url and (force_refresh or url_changed or not (t.title or "").strip() or not t.duration_seconds or not (t.description or "").strip()):
        try:
            meta_title, meta_len, _thumb, meta_desc = _youtube_meta_safe(t.video_url)

            if force_refresh:
                if meta_title:
                    t.title = meta_title
                if meta_len:
                    t.duration_seconds = meta_len
                if meta_desc:
                    t.description = meta_desc[:4000]
                else:
                    a_name, a_url = _youtube_oembed_author(t.video_url)
                    if a_url:
                        who = a_name or "this creator"
                        t.description = f"Subscribe to: {who} — {a_url}"
            else:
                if (not (t.title or "").strip()) and meta_title:
                    t.title = meta_title
                if (not t.duration_seconds) and meta_len:
                    t.duration_seconds = meta_len
                if (not (t.description or "").strip()):
                    if meta_desc:
                        t.description = meta_desc[:4000]
                    else:
                        a_name, a_url = _youtube_oembed_author(t.video_url)
                        if a_url:
                            who = a_name or "this creator"
                            t.description = f"Subscribe to: {who} — {a_url}"
        except Exception:
            pass

    t.save()
    return JsonResponse({"success": True})


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

# ============================ Open CV Image Processing ==========================================

class VisionDetectView(views.APIView):
    permission_classes = [IsStaffish]
    parser_classes = [MultiPartParser]

    def post(self, request):
        image = request.FILES.get("image")
        dropoff_site_id = request.data.get("dropoff_site_id")
        if not image or not dropoff_site_id:
            return Response({"success": False, "error": "Missing image or dropoff_site_id."}, status=400)

        try:
            # 1) Run detection ONCE
            items, counts, w, h = _run_yolo_and_summarize(image)
        except Exception:
            LOGGER.exception("YOLO inference failed")
            return Response({"success": False, "error": "Image analysis failed."}, status=400)

        # 2) Rewind before saving the same file object to the model field
        try:
            image.seek(0)
        except Exception:
            pass

        # 3) Persist detection session for later edits/confirm
        session = DetectionSession.objects.create(
            staff=request.user,
            image=image,
            items=items,
            width=w,
            height=h,
        )

        # 4) Prepare default bottle_data + points
        bottle_data = [
            {"size": "small", "count": int(counts.get("small", 0))},
            {"size": "large", "count": int(counts.get("large", 0))},
        ]
        proposed_points = compute_points(bottle_data)

        return Response({
            "success": True,
            "session_id": session.id,
            "image_url": request.build_absolute_uri(session.image.url),
            "auto_counts": counts,
            "bottle_data": bottle_data,
            "proposed_points": proposed_points,
        }, status=201)

        

class VisionConfirmView(views.APIView):
    permission_classes = [IsStaffish]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def post(self, request):
        try:
            session_id = int(request.data.get("session_id"))
            dropoff_site_id = int(request.data.get("dropoff_site_id"))
        except Exception:
            return Response({"success": False, "error": "Invalid session_id or dropoff_site_id."}, status=400)

        # Allow bottle_data override from UI (editable)
        edited = request.data.get("bottle_data")
        if isinstance(edited, str):
            try:
                edited = json.loads(edited)
            except Exception:
                edited = None

        session = get_object_or_404(DetectionSession, pk=session_id, staff=request.user)
        site = get_object_or_404(DropOffSite, pk=dropoff_site_id)

        if not edited or not isinstance(edited, list):
            small = sum(1 for i in (session.items or []) if "small" in (i.get("cls_name","").lower()))
            large = sum(1 for i in (session.items or []) if "large" in (i.get("cls_name","").lower()))
            bottle_data = [{"size": "small", "count": small}, {"size": "large", "count": large}]
        else:
            bottle_data = []
            for it in edited:
                size = (it.get("size") or "").lower()
                if size in {"small", "large"}:
                    try:
                        cnt = int(it.get("count", 0))
                    except Exception:
                        cnt = 0
                    bottle_data.append({"size": size, "count": max(0, cnt)})

        proposed_points = compute_points(bottle_data)
        qr_token = secrets.token_urlsafe(24)
        expires = timezone.now() + timedelta(hours=1)

        with transaction.atomic():
            sub = Submission.objects.create(
                staff=request.user,
                dropoff_site=site,
                bottle_data=bottle_data,
                proposed_points=proposed_points,
                claimed_points=0,
                qr_token=qr_token,
                qr_expires_at=expires,
                status="pending",
                source="vision",
                image=session.image,  # keep original photo
                estimated_quantity=sum(int(x["count"]) for x in bottle_data),
                confidence_score=None,
            )
            StaffTransaction.objects.create(
                staff=request.user, submission=sub, action="submission_created", notes="via vision"
            )
            session.delete()

        return Response({
            "success": True,
            "submission_id": sub.id,
            "proposed_points": proposed_points,
            "qr_token": qr_token,
            "qr_expires_at": expires.isoformat(),
        }, status=201)



# ---- DIY submission (mobile/user uploads proof) ----
class DIYSubmitView(views.APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsResident]
    parser_classes = [MultiPartParser]

    def post(self, request):
        ser = DIYSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user: User = request.user
        tutorial = get_object_or_404(DIYTutorial, pk=ser.validated_data["tutorial_id"])

        # 1) Enforce cooldown: block if user submitted this DIY within N days
        cutoff = _cooldown_cutoff()
        if DIYSubmission.objects.filter(
            user=user, tutorial=tutorial, created_at__date__gte=cutoff
        ).exists():
            return Response(
                {
                    "success": False,
                    "error": f"You’ve already submitted this DIY recently. Try again after {_cooldown_days()} days.",
                    "cooldown_days": _cooldown_days(),
                },
                status=400,
            )

        # 2) Enforce "must be in this week's rotation" to re-submit
        anchor = _period_anchor(today_ph())
        in_this_week = DIYDailySelection.objects.filter(date=anchor, tutorial=tutorial).exists()
        if not in_this_week:
            return Response(
                {"success": False, "error": "This DIY is not in this week’s selection."},
                status=400,
            )

        # 3) (Optional safety) if they submitted a long time ago (beyond cooldown),
        #    allow again (we do, since the check above passed).

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
    permission_classes = [IsResident]

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
    Re-auth by asking ONLY the current logged-in admin for their password.
    Accepts form-encoded or JSON: { "password": "..." }.
    On success, sets a 5-minute session flag 'points_edit_ok_until'.
    """
    # Dev bypass (shared key / flag)
    if _admin_bypass_ok(request):
        request.session["points_edit_ok_until"] = (timezone.now() + timedelta(minutes=5)).isoformat()
        return JsonResponse({"success": True, "until": request.session["points_edit_ok_until"]})

    # Must already be logged in as a superuser to even attempt reauth.
    u = request.user
    if not (u.is_authenticated and u.is_active and u.is_superuser):
        return JsonResponse({"success": False, "error": "Admin session required."}, status=401)

    # Read password from form or JSON
    password = request.POST.get("password")
    if password is None:  # maybe JSON
        try:
            payload = json.loads((request.body or b"").decode("utf-8") or "{}")
        except Exception:
            payload = {}
        password = payload.get("password")

    if not password:
        return JsonResponse({"success": False, "error": "Missing password."}, status=400)

    # Check the current admin's password directly
    if not u.check_password(password):
        return JsonResponse({"success": False, "error": "Invalid admin credentials."}, status=403)

    # Success → grant a short-lived edit window
    request.session["points_edit_ok_until"] = (timezone.now() + timedelta(minutes=5)).isoformat()
    return JsonResponse({"success": True, "until": request.session["points_edit_ok_until"]})


class PointsConfigView(views.APIView):
    authentication_classes = [TokenAuthentication, SessionAuthentication, BasicAuthentication]

    # GET: any real staff (or admin-bypass) can read
    # POST: only admin can update
    def get_permissions(self):
        if self.request.method == "GET":
            return [IsStaffish()]   # staff can read the config
        return [IsAdminOnly()]      # only admins can edit

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

        token, _ = Token.objects.get_or_create(user=user)

        # find (or infer) their site
        site = DropOffSite.objects.filter(staff_members=user).first()
        if not site and user.barangay:
            # safety net: ensure site exists for their barangay
            site, _ = DropOffSite.objects.get_or_create(barangay=user.barangay)

        return Response({"success": True, "token": token.key, "user": {
            "id": user.id,
            "name": user.get_full_name(),
            "email": user.email,
            "username": user.username,
            "mobile": user.mobile_number,
            "barangay": user.barangay.name if user.barangay else None,
            "dropoff_site_id": site.id if site else None,   # <<< add this
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

        # 🚫 block staff accounts entirely from resident app
        if user.is_staff:
            return Response({"success": False, "error": "Use the staff app to sign in."}, status=403)

        # must already be a resident
        if not user.groups.filter(name="resident").exists():
            return Response({"success": False, "error": "This account is not a resident account."}, status=403)

        user_auth = authenticate(username=user.username, password=password)
        if not user_auth:
            return Response({"success": False, "error": "Incorrect password."}, status=400)

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
        expires = timezone.now() + timedelta(hours=1)

        with transaction.atomic():
            sub = Submission.objects.create(
                staff=staff,
                dropoff_site=dropoff_site,
                bottle_data=bottle_data,
                proposed_points=proposed_points,
                qr_token=qr_token,
                qr_expires_at=expires,   # <- 1h from now
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

@method_decorator(csrf_exempt, name="dispatch")   # image loads with no cookies/headers
class SubmissionQRView(View):
    """GET /api/submissions/<id>/qr.png?sig=<signed> → QR PNG (only while valid)"""
    def get(self, request, submission_id: int):
        sig = request.GET.get("sig", "")
        if not sig:
            return HttpResponse("Missing signature.", status=403)

        # Verify signature is <= 1 hour old
        try:
            payload = signer.unsign(sig, max_age=3600)  # 1 hour window
        except SignatureExpired:
            return HttpResponse("QR link expired.", status=410)
        except BadSignature:
            return HttpResponse("Invalid QR link.", status=403)

        # payload = "<id>:<qr_token>"
        try:
            id_str, token = payload.split(":", 1)
        except ValueError:
            return HttpResponse("Invalid payload.", status=403)
        if str(submission_id) != id_str:
            return HttpResponse("Mismatched submission id.", status=403)

        try:
            sub = Submission.objects.only("qr_token", "qr_expires_at", "status").get(pk=submission_id)
        except Submission.DoesNotExist:
            raise Http404

        # Token must still match the DB (guards against reuse after rotation)
        if sub.qr_token != token:
            return HttpResponse("Invalid token.", status=403)

        # Enforce server-side status/expiry as well
        now = timezone.now()
        if sub.status == "pending" and sub.qr_expires_at and sub.qr_expires_at < now:
            Submission.objects.filter(pk=submission_id, status="pending").update(status="expired")
            return HttpResponse("QR expired.", status=410)

        if sub.status != "pending":
            return HttpResponse("QR not available.", status=410)

        try:
            import qrcode
        except ImportError:
            return HttpResponse("QR code generator not installed.", status=500)

        img = qrcode.make(sub.qr_token)
        buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
        return HttpResponse(buf.read(), content_type="image/png")



class SubmissionClaimView(views.APIView):
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsResident]   # ⬅️ only residents

    parser_classes = [JSONParser]

    def post(self, request):
        ser = SubmissionClaimSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        token = ser.validated_data["qr_token"]

        # (optional defense-in-depth)
        if request.user.is_staff:
            return Response({"success": False, "error": "Staff cannot claim via resident endpoint."}, status=403)

        with transaction.atomic():
            sub = Submission.objects.select_for_update().filter(qr_token=token).first()
            if not sub:
                return Response({"success": False, "error": "Invalid QR."}, status=404)

            if sub.status != "pending":
                if sub.status == "claimed" and sub.claimed_by_id == request.user.id:
                    return Response(
                        {"success": True, "claimed_points": sub.claimed_points, "balance": request.user.total_points},
                        status=200
                    )
                return Response({"success": False, "error": "QR already used or invalid state."}, status=400)

            if sub.qr_expires_at and sub.qr_expires_at < timezone.now():
                if sub.status == "pending":
                    sub.status = "expired"
                    sub.save(update_fields=["status"])
                return Response({"success": False, "error": "QR expired."}, status=400)

            sub.claimed_by = request.user
            sub.claimed_at = timezone.now()
            sub.claimed_points = sub.proposed_points
            sub.status = "claimed"
            sub.save(update_fields=["claimed_by","claimed_at","claimed_points","status","updated_at"])

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
        # daily buckets with ISO keys
        c = Counter([localtime(s.created_at).date().strftime("%Y-%m-%d") for s in qs])
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


# --- Deactivate / Reactivate / Hard delete (admin only) ----------------------

@require_POST
@require_admin_json
def deactivate_staff_json(request, user_id: int):
    """
    Soft-disable a staff member but KEEP all their data & submissions.
    - Removes them from any DropOffSite assignments
    - Flips is_active=False and is_approved=False
    """
    staff = get_object_or_404(User, pk=user_id)
    with transaction.atomic():
        remove_staff_from_all_sites(staff)
        staff.is_active = False
        staff.is_approved = False
        staff.save(update_fields=["is_active", "is_approved"])
    return JsonResponse({"success": True})

@require_POST
@require_admin_json
def reactivate_staff_json(request, user_id: int):
    """
    Re-enable a previously deactivated staff.
    Leaves barangay unchanged, simply flips flags on.
    """
    staff = get_object_or_404(User, pk=user_id)
    with transaction.atomic():
        staff.is_active = True
        staff.is_approved = True
        staff.is_staff = True
        staff.groups.add(get_or_create_group("staff"))
        staff.save(update_fields=["is_active", "is_approved", "is_staff"])
        # Optional: auto-ensure site if they still have a barangay
        if staff.barangay_id:
            ensure_site_and_add_staff(staff.barangay, staff)
    return JsonResponse({"success": True})

@require_POST
@require_admin_json
def hard_delete_staff_json(request, user_id: int):
    """
    Optional dangerous op: actually delete the staff account.
    NOTE: Submissions remain since they belong to Submission.staff (FK) — if your FK
    is PROTECT you’ll get an error; if it’s SET_NULL, you’ll keep history but without a user.
    Prefer 'deactivate' in almost all cases.
    """
    staff = get_object_or_404(User, pk=user_id)
    with transaction.atomic():
        remove_staff_from_all_sites(staff)
        staff.delete()
    return JsonResponse({"success": True})



@require_admin_page
@ensure_csrf_cookie
def staff_management_view(request):
    pending_reqs = (
        StaffApprovalRequest.objects
        .select_related("user", "requested_barangay")
        .filter(status="pending")
        .order_by("created_at")
    )

    active_staff = (
        User.objects
        .filter(is_approved=True, is_staff=True, is_active=True, is_superuser=False)
        .order_by("date_joined")
    )

    inactive_staff = (
        User.objects
        .filter(is_staff=True, is_active=False, is_superuser=False)
        .order_by("date_joined")
    )

    return render(
        request,
        "staff_management.html",
        {
            "pending_reqs": pending_reqs,
            "active_staff": active_staff,
            "inactive_staff": inactive_staff,  # <-- new
        },
    )

    


@require_admin_page
@ensure_csrf_cookie
def submissions_admin_view(request):
    cfg = PointsConfig.current()

    if request.method == "POST":
        until_iso = request.session.get("points_edit_ok_until")
        can_edit = False
        if until_iso:
            until_dt = parse_datetime(until_iso)
            if until_dt:
                if timezone.is_naive(until_dt):
                    until_dt = timezone.make_aware(until_dt, timezone.get_current_timezone())
                if until_dt > timezone.now():
                    can_edit = True

        if not can_edit:
            sites = (DropOffSite.objects.select_related("barangay")
                     .prefetch_related("staff_members").order_by("barangay__name"))
            return render(request, "submissions_admin.html", {
                "cfg": cfg,
                "sites": sites,
                "points_edit_error": "Re-auth as superuser required before editing.",
            })

        # proceed with saving
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

        # lock again and redirect (PRG)
        request.session.pop("points_edit_ok_until", None)
        messages.success(request, "Points updated.")
        return redirect("dropoff_sites_view")   # or redirect(request.path)

    sites = (DropOffSite.objects.select_related("barangay")
             .prefetch_related("staff_members").order_by("barangay__name"))
    return render(request, "submissions_admin.html", {"cfg": cfg, "sites": sites})





@require_admin_page
def submissions_by_dropoff_site(request, site_id: int):
    site = get_object_or_404(DropOffSite, id=site_id)
    submissions = Submission.objects.filter(dropoff_site=site).select_related("staff", "claimed_by").order_by("-created_at")
    return render(request, "submissions_by_site.html", {"site": site, "submissions": submissions})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication, BasicAuthentication])
@permission_classes([IsAdminOrStaff])
def staff_transaction_history(request, staff_id: int):
    """
    GET /api/staff/<staff_id>/transactions/
    - Admin: can view any staff history
    - Staff: can only view their own history
    - Mobile app: use "Authorization: Token <key>"
    - Admin page: uses session cookie automatically
    """
    # --- scope: staff can only see their own
    if not (_admin_bypass_ok(request) or _is_admin_only(request.user)):
        if not (request.user.is_authenticated and request.user.id == staff_id):
            return Response({"success": False, "error": "Forbidden."}, status=403)

    # verify staff exists
    staff = get_object_or_404(User, pk=staff_id)
    admin_key = getattr(settings, "ADMIN_SHARED_KEY", None)

    txs = (
        StaffTransaction.objects
        .filter(staff_id=staff_id)
        .select_related("submission")
        .order_by("-created_at")
    )

    now = timezone.now()
    items = []
    for t in txs:
        sub = t.submission
        sub_dict = None
        points_for_row = 0

        if sub:
            # --- Auto-expire if pending & past expiry ---
            if sub.status == "pending" and sub.qr_expires_at and sub.qr_expires_at < now:
                Submission.objects.filter(pk=sub.id, status="pending").update(status="expired")
                sub.status = "expired"

            # --- Choose the number to show in the list ---
            # claimed → claimed_points; pending → proposed_points; expired → proposed_points
            points_for_row = int((sub.claimed_points if sub.status == "claimed" else sub.proposed_points) or 0)


            qr_url = None
            if sub.status == "pending":
                signed = signer.sign(f"{sub.id}:{sub.qr_token}")
                qr_url = request.build_absolute_uri(
                    reverse("submission_qr", args=[sub.id])
                ) + f"?sig={signed}"

            sub_dict = {
                "id": sub.id,
                "status": sub.status,
                "created_at": localtime(sub.created_at).isoformat(),
                "claimed_at": (localtime(sub.claimed_at).isoformat() if sub.claimed_at else None),
                "qr_expires_at": (localtime(sub.qr_expires_at).isoformat() if sub.qr_expires_at else None),
                "claimed_points": int(sub.claimed_points or 0),
                "proposed_points": int(sub.proposed_points or 0),
                "bottle_data": sub.bottle_data or [],
                "qr_url": qr_url,
                "qr_token": (sub.qr_token if sub.status == "pending" else None),
                "display_points": int((sub.claimed_points if sub.status == "claimed" else sub.proposed_points) or 0),
            }

        items.append({
            "action": t.action,
            "notes": t.notes or "",
            # CHANGED: use points_for_row so the app list shows correct number
            "points": int(points_for_row),
            "date": timezone.localtime(t.created_at).strftime("%Y-%m-%d %H:%M"),
            "submission": sub_dict,
        })

    return Response({"success": True, "transactions": items})



def _period_bounds(param: str) -> Tuple[date, date, str, List[str]]:
    today = today_ph()
    param = (param or "week").lower()

    if param == "year":
        start = date(today.year, 1, 1)
        end   = date(today.year + 1, 1, 1)
        labels = [f"{today.year}-{m:02d}" for m in range(1, 13)]  # YYYY-MM
        return start, end, "year", labels

    if param == "month":
        start = date(today.year, today.month, 1)
        end = date(today.year + (1 if today.month == 12 else 0),
                   1 if today.month == 12 else today.month + 1, 1)
        last_dom = monthrange(today.year, today.month)[1]
        labels = [f"{today.year}-{today.month:02d}-{d:02d}" for d in range(1, last_dom + 1)]  # YYYY-MM-DD
        return start, end, "month", labels

    # week (Mon..next Mon)
    sow = today - timedelta(days=today.weekday())
    start = sow
    end   = sow + timedelta(days=7)
    labels = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]  # YYYY-MM-DD
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
    
    for s in submissions:
        small = large = 0
        for b in (s.bottle_data or []):
            size = (b.get("size") or "").lower()
            try:
                cnt = int(b.get("count") or b.get("quantity") or 0)
            except Exception:
                cnt = 0
            if size == "small":
                small += cnt
            elif size == "large":
                large += cnt
        s.small_count = small
        s.large_count = large

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
            localtime(dt).date().strftime("%Y-%m-%d")
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