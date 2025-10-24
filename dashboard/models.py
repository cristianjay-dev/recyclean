# dashboard/models.py
from __future__ import annotations

import secrets  # NEW: for QR token generator

from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, RegexValidator
from django.contrib.auth.models import AbstractUser
from django.db.models.functions import Lower


# =============================================================
#  Helpers
# =============================================================

def generate_qr_token() -> str:
    """Random, URL-safe token for QR codes (≈32 chars)."""
    return secrets.token_urlsafe(24)


# =============================================================
#  Top-level (no FK) models first
# =============================================================

class Barangay(models.Model):
    """Tacloban barangays (admin-managed list)."""
    name = models.CharField(max_length=150, unique=True)
    city = models.CharField(max_length=150, default="Tacloban City")

    class Meta:
        db_table = "barangays"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["name", "city"], name="uniq_barangay_name_city"),
        ]

    def __str__(self) -> str:
        return f"{self.name}, {self.city}"


# ---- Username validation (3–20, start letter, letters/digits/_ only) ----
username_validator = RegexValidator(
    regex=r'^[A-Za-z][A-Za-z0-9_]{2,19}$',
    message="Username must start with a letter and contain only letters, numbers, or underscores (3–20 chars).",
)


class User(AbstractUser):
    """
    Custom user model.
    - Use Django Groups ('resident', 'staff') to differentiate roles.
    - Staff accounts are approved via StaffApprovalRequest.
    - Residents are active immediately on signup.
    """

    # Override AbstractUser.username to enforce our rules (and shorter length)
    username = models.CharField(
        max_length=20,
        unique=True,
        validators=[username_validator],
        help_text="3–20 chars; start with a letter; letters, numbers, and underscores only.",
    )

    mobile_number = models.CharField(max_length=20, unique=True, null=True, blank=True)
    barangay = models.ForeignKey(Barangay, on_delete=models.SET_NULL, null=True, blank=True, related_name="users")

    is_approved = models.BooleanField(default=False)

    ACCOUNT_STATUS_CHOICES = (("active", "Active"), ("disabled", "Disabled"))
    account_status = models.CharField(max_length=10, choices=ACCOUNT_STATUS_CHOICES, default="active")

    registered_by_admin = models.BooleanField(default=False)
    weekly_bonus_given = models.DateField(null=True, blank=True)

    total_points = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])

    class Meta:
        db_table = "users"
        ordering = ["username"]
        indexes = [
            models.Index(fields=["username"]),
            models.Index(fields=["email"]),
            models.Index(fields=["mobile_number"]),
        ]
        # Case-insensitive uniqueness at DB level
        constraints = [
            models.UniqueConstraint(
                Lower("username"),
                name="uniq_users_username_ci",
            ),
        ]

    def __str__(self) -> str:
        return self.get_full_name() or self.username


# =============================================================
#  FK-bearing models
# =============================================================

# dashboard/models.py

class DropOffSite(models.Model):
    """One drop-off site per barangay; multiple staff can be assigned."""
    barangay = models.OneToOneField(
        Barangay, on_delete=models.PROTECT, related_name="dropoff_site"
    )
    staff_members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="assigned_sites",
    )

    class Meta:
        db_table = "dropoff_sites"

    def __str__(self) -> str:
        return f"{self.barangay.name} Drop-Off Site"



# --------- Adjustable Points (singleton) ---------

class PointsConfig(models.Model):
    """
    Global, adjustable bottle points (small/large).
    Enforced singleton via a constant unique field.
    """
    singleton = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    small_bottle_points = models.PositiveIntegerField(
        default=5, validators=[MinValueValidator(0)], help_text="Points per SMALL bottle"
    )
    large_bottle_points = models.PositiveIntegerField(
        default=10, validators=[MinValueValidator(0)], help_text="Points per LARGE bottle"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "points_config"

    def __str__(self) -> str:
        return f"PointsConfig(small={self.small_bottle_points}, large={self.large_bottle_points})"

    def save(self, *args, **kwargs):
        # Always keep singleton marker = 1 so only one row can exist
        self.singleton = 1
        super().save(*args, **kwargs)

    @classmethod
    def current(cls) -> "PointsConfig":
        obj, _ = cls.objects.get_or_create(singleton=1, defaults={})
        return obj


class Submission(models.Model):
    """
    Bottle drop-off created by staff, claimed by resident via QR.
    bottle_data = [{"size": "small"|"large", "count": int}, ...]
    """
    SOURCE_CHOICES = (("manual", "Manual"), ("vision", "Vision"))
    STATUS_CHOICES = (("pending", "Pending"), ("claimed", "Claimed"), ("expired", "Expired"), ("voided", "Voided"))

    # Optional resident linked at intake (can be null until claim); the claimer is stored in 'claimed_by'.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="submissions"
    )
    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="processed_submissions"
    )
    dropoff_site = models.ForeignKey(DropOffSite, on_delete=models.PROTECT, null=True, related_name="submissions")

    bottle_data = models.JSONField(default=list, blank=True)  # [{"size":"small|large","count":N}]
    proposed_points = models.PositiveIntegerField(default=0)
    claimed_points = models.PositiveIntegerField(default=0)

    # NEW: default generator so migrations can populate existing rows
    qr_token = models.CharField(
        max_length=64,
        unique=True,
        default=generate_qr_token,
        editable=False,
    )
    qr_expires_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default="manual")

    image = models.ImageField(upload_to="submissions/", blank=True, null=True)

    claimed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="claimed_submissions"
    )
    claimed_at = models.DateTimeField(null=True, blank=True)

    # (Optional) for future vision use
    estimated_quantity = models.PositiveIntegerField(blank=True, null=True)
    confidence_score = models.FloatField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "submissions"
        indexes = [
            models.Index(fields=["qr_token"]),
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["claimed_by"]),
            models.Index(fields=["dropoff_site"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        who = getattr(self.claimed_by or self.user, "username", None) or "Unclaimed"
        return f"Submission({who}, {self.proposed_points}->{self.claimed_points} pts, {self.status})"

class DetectionSession(models.Model):
    """
    Temporary store for staff-uploaded image and raw YOLO results prior to confirm.
    Staff edits happen on these 'items', then we create a real Submission from them.
    """
    staff  = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="vision_sessions")
    image  = models.ImageField(upload_to="detections/%Y/%m/%d/")   # original photo
    items  = models.JSONField(default=list, blank=True)            # [{cls_name, conf, bbox, polygon?, area_frac}]
    width  = models.PositiveIntegerField(null=True, blank=True)    # image width at detect time
    height = models.PositiveIntegerField(null=True, blank=True)    # image height at detect time
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "vision_detection_sessions"
        ordering = ["-created_at"]

    def __str__(self):
        return f"DetectSession {self.id} by {self.staff_id}"


class StaffTransaction(models.Model):
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="staff_transactions")
    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="transaction_record")
    action = models.CharField(max_length=50, default="submission_created")
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "staff_transactions"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"StaffTransaction({self.staff_id}, {self.action}, sub={self.submission_id})"


class UserPointsLedger(models.Model):
    SOURCE_CHOICES = (("submission_claim", "Submission Claim"), ("diy_submit", "DIY Submission"), ("adjustment", "Adjustment"))

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="points_ledger")
    submission = models.ForeignKey(Submission, on_delete=models.SET_NULL, null=True, blank=True, related_name="ledger_entries")
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    delta_points = models.IntegerField()
    balance_after = models.PositiveIntegerField(default=0)
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_points_ledger"
        indexes = [models.Index(fields=["user", "created_at"])]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Ledger({self.user_id}: {self.delta_points} → {self.balance_after})"


PH_LOCAL_PHONE_VALIDATOR = RegexValidator(
    regex=r'^0\d{10}$',
    message="PH mobile must be local format 09XXXXXXXXX (11 digits).",
)
class RewardRequest(models.Model):
    TELCO_CHOICES = (("globe", "Globe"), ("smart", "Smart"), ("dito", "DITO"), ("other", "Other"))
    STATUS_CHOICES = (("requested", "Requested"), ("paid", "Paid"), ("rejected", "Rejected"))

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reward_requests")

    # Store canonical local 09...; inputs like +63... are normalized in views/services.
    mobile_number = models.CharField(max_length=20, validators=[PH_LOCAL_PHONE_VALIDATOR])

    telco = models.CharField(max_length=10, choices=TELCO_CHOICES, default="other")
    operator_id = models.IntegerField(null=True, blank=True)
    operator_name = models.CharField(max_length=100, null=True, blank=True)

    points_used = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="requested")
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="processed_rewards"
    )

    custom_identifier = models.CharField(max_length=100, null=True, blank=True)
    reloadly_tx_id = models.CharField(max_length=100, null=True, blank=True)
    reloadly_raw = models.JSONField(null=True, blank=True)
    last_error = models.TextField(null=True, blank=True)

    date_requested = models.DateTimeField(auto_now_add=True)
    date_processed = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "reward_requests"
        ordering = ["-date_requested"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["date_requested"]),
            models.Index(fields=["mobile_number"]),
            # composite for webhook match (filters mobile_number, amount, status; then DB can sort by date)
            models.Index(
                fields=["mobile_number", "amount", "status", "date_requested"],
                name="rwreq_webhook_match_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"RewardRequest({self.user_id}, {self.amount} PHP, {self.status})"


class StaffApprovalRequest(models.Model):
    STATUS_CHOICES = (("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected"))

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="staff_approval")
    requested_barangay = models.ForeignKey(Barangay, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    notes = models.TextField(blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="decided_staff_requests"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "staff_approval_requests"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"StaffApprovalRequest({self.user_id}, {self.status})"


# =============================================================
#  DIY Tutorials (replaces Quiz)
# =============================================================

class DIYTutorial(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField(help_text="Instructions and materials list.")
    video_url = models.URLField()
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    thumbnail = models.ImageField(upload_to="diy_thumbs/", null=True, blank=True)
    points_on_submit = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "diy_tutorials"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title


class DIYDailyPool(models.Model):
    """Admin-curated pool of tutorials for a given date (from which one is picked)."""
    date = models.DateField(unique=True)
    tutorials = models.ManyToManyField(DIYTutorial, related_name="daily_pools")

    class Meta:
        db_table = "diy_daily_pools"
        ordering = ["-date"]

    def __str__(self) -> str:
        return f"DIY Pool for {self.date}"


# models.py
class DIYDailySelection(models.Model):
    """The tutorials selected for a given date (now supports multiple per day)."""
    date = models.DateField(db_index=True)  # was unique=True
    pool = models.ForeignKey(DIYDailyPool, on_delete=models.CASCADE, null=True, blank=True, related_name="selections")
    tutorial = models.ForeignKey(DIYTutorial, on_delete=models.PROTECT, related_name="daily_selections")
    selected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "diy_daily_selections"
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(fields=["date", "tutorial"], name="uniq_daily_tutorial"),
        ]

    def __str__(self) -> str:
        return f"DIYDailySelection({self.date}: {self.tutorial})"



class DIYSubmission(models.Model):
    """User’s optional photo proof for a DIY; awards points once."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="diy_submissions")
    tutorial = models.ForeignKey(DIYTutorial, on_delete=models.CASCADE, related_name="user_submissions")
    image = models.ImageField(upload_to="diy_submissions/", null=True, blank=True)
    caption = models.CharField(max_length=280, blank=True)
    is_public = models.BooleanField(default=True)
    approved = models.BooleanField(default=True)
    points_awarded = models.PositiveIntegerField(default=0)
    awarded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "diy_submissions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "tutorial", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"DIYSubmission by {self.user_id} on {self.tutorial_id}"