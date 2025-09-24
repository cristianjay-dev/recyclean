from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils import timezone
from django.db import transaction

from .models import (
    Barangay,
    User,
    DropOffSite,
    Submission,
    StaffTransaction,
    UserPointsLedger,
    RewardRequest,
    StaffApprovalRequest,
    DIYTutorial,
    DIYDailyPool,
    DIYDailySelection,
    DIYSubmission,
    PointsConfig,
)

# =============== Utility helpers ===============

def _add_to_group(user, group_name: str):
    from django.contrib.auth.models import Group
    grp, _ = Group.objects.get_or_create(name=group_name)
    user.groups.add(grp)

# =============== Points Config ===============

@admin.register(PointsConfig)
class PointsConfigAdmin(admin.ModelAdmin):
    list_display = ("small_bottle_points", "large_bottle_points", "updated_at")
    readonly_fields = ("updated_at",)
    fields = ("small_bottle_points", "large_bottle_points", "updated_at")

    def has_add_permission(self, request):
        # Keep a single row (edit-only singleton)
        return not PointsConfig.objects.exists()

# =============== User & related ===============

class UserPointsLedgerInline(admin.TabularInline):
    model = UserPointsLedger
    extra = 0
    can_delete = False
    readonly_fields = ("source", "delta_points", "balance_after", "notes", "created_at", "submission")

    def has_add_permission(self, request, obj=None):
        return False

@admin.register(User)
class UserAdmin(BaseUserAdmin):
    # columns
    list_display = (
        "username", "email", "first_name", "last_name",
        "mobile_number", "get_barangay", "is_approved", "is_active", "is_staff", "total_points",
    )
    list_filter = ("is_approved", "is_active", "is_staff", "groups")
    search_fields = ("username", "email", "first_name", "last_name", "mobile_number", "barangay__name")
    ordering = ("username",)
    inlines = [UserPointsLedgerInline]

    # fields grouping
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email", "mobile_number", "barangay")}),
        ("Status", {"fields": ("is_approved", "account_status", "total_points")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("username", "email", "first_name", "last_name", "mobile_number", "barangay", "password1", "password2"),
        }),
    )

    actions = ["approve_selected_staff", "add_to_resident_group", "add_to_staff_group"]

    @admin.display(description="Barangay")
    def get_barangay(self, obj):
        return getattr(getattr(obj, "barangay", None), "name", None)

    @transaction.atomic
    def approve_selected_staff(self, request, queryset):
        """
        Mark selected users as approved + active and ensure they’re in 'staff' group.
        Also attach them to the DropOffSite for their barangay (create if needed).
        """
        updated = 0
        for user in queryset:
            if not user.is_approved:
                user.is_approved = True
                user.is_active = True
                user.save(update_fields=["is_approved", "is_active"])
                _add_to_group(user, "staff")
                if user.barangay:
                    site, _ = DropOffSite.objects.get_or_create(barangay=user.barangay)
                    site.staff_members.add(user)  # M2M now
                updated += 1
        self.message_user(request, f"Approved {updated} staff account(s).")
    approve_selected_staff.short_description = "Approve selected as staff (activate + link to site)"

    def add_to_resident_group(self, request, queryset):
        for u in queryset:
            _add_to_group(u, "resident")
        self.message_user(request, "Added selected users to 'resident' group.")
    add_to_resident_group.short_description = "Add to resident group"

    def add_to_staff_group(self, request, queryset):
        for u in queryset:
            _add_to_group(u, "staff")
        self.message_user(request, "Added selected users to 'staff' group.")
    add_to_staff_group.short_description = "Add to staff group"

# =============== Barangay & DropOffSite ===============

@admin.register(Barangay)
class BarangayAdmin(admin.ModelAdmin):
    list_display = ("name", "city")
    search_fields = ("name", "city")
    ordering = ("name",)

@admin.register(DropOffSite)
class DropOffSiteAdmin(admin.ModelAdmin):
    list_display = ("barangay", "staff_count", "staff_list")
    search_fields = (
        "barangay__name",
        "staff_members__username",
        "staff_members__email",
        "staff_members__first_name",
        "staff_members__last_name",
    )
    list_filter = ("barangay",)
    filter_horizontal = ("staff_members",)  # nice UI for M2M pick

    @admin.display(description="Staff #")
    def staff_count(self, obj):
        return obj.staff_members.count()

    @admin.display(description="Staff Members")
    def staff_list(self, obj):
        names = [u.get_full_name() or u.username for u in obj.staff_members.all()]
        return ", ".join(sorted(names))

# =============== Submissions & Transactions ===============

@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "id", "dropoff_site", "staff", "claimed_by",
        "proposed_points", "claimed_points", "status", "source",
        "created_at", "qr_expires_at",
    )
    list_filter = ("status", "source", "dropoff_site__barangay")
    search_fields = (
        "staff__username", "staff__email", "claimed_by__username", "claimed_by__email",
        "dropoff_site__barangay__name", "qr_token",
    )
    readonly_fields = ("created_at", "updated_at", "claimed_at")
    date_hierarchy = "created_at"

@admin.register(StaffTransaction)
class StaffTransactionAdmin(admin.ModelAdmin):
    list_display = ("id", "staff", "submission", "action", "created_at")
    search_fields = ("staff__username", "staff__email", "submission__id")
    list_filter = ("action",)
    date_hierarchy = "created_at"

@admin.register(UserPointsLedger)
class UserPointsLedgerAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "source", "delta_points", "balance_after", "created_at", "submission")
    search_fields = ("user__username", "user__email", "notes")
    list_filter = ("source",)
    readonly_fields = ("created_at",)

# =============== Rewards (Reloadly) ===============

@admin.register(RewardRequest)
class RewardRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "mobile_number", "amount", "points_used", "status", "date_requested", "date_processed")
    list_filter = ("status",)
    search_fields = ("user__username", "user__email", "mobile_number")
    readonly_fields = ("date_requested",)
    actions = ["mark_paid", "mark_rejected"]

    @transaction.atomic
    def mark_paid(self, request, queryset):
        """
        Admin override to mark as paid and deduct points if not yet deducted.
        This does not call Reloadly; it only adjusts local records.
        """
        changed = 0
        for rr in queryset:
            if rr.status != "paid":
                user = rr.user
                if user.total_points >= rr.points_used:
                    user.total_points -= rr.points_used
                    user.save(update_fields=["total_points"])
                rr.status = "paid"
                rr.date_processed = timezone.now()
                rr.save(update_fields=["status", "date_processed"])
                changed += 1
        self.message_user(request, f"Marked {changed} request(s) as paid.")
    mark_paid.short_description = "Mark selected as PAID (deduct points)"

    @transaction.atomic
    def mark_rejected(self, request, queryset):
        changed = queryset.exclude(status="rejected").update(status="rejected", date_processed=timezone.now())
        self.message_user(request, f"Marked {changed} request(s) as rejected.")
    mark_rejected.short_description = "Mark selected as REJECTED"

# =============== Staff Approval Requests ===============

@admin.register(StaffApprovalRequest)
class StaffApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "requested_barangay", "status", "created_at", "decided_by", "decided_at")
    list_filter = ("status", "requested_barangay")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("created_at", "updated_at")
    actions = ["approve_requests", "reject_requests"]

    @transaction.atomic
    def approve_requests(self, request, queryset):
        count = 0
        for req in queryset.select_related("user", "requested_barangay"):
            if req.status != "approved":
                user = req.user
                user.is_approved = True
                user.is_active = True
                if req.requested_barangay and not user.barangay:
                    user.barangay = req.requested_barangay
                user.save(update_fields=["is_approved", "is_active", "barangay"])
                _add_to_group(user, "staff")

                if user.barangay:
                    site, _ = DropOffSite.objects.get_or_create(barangay=user.barangay)
                    site.staff_members.add(user)  # M2M now

                req.status = "approved"
                req.decided_by = request.user
                req.decided_at = timezone.now()
                req.save(update_fields=["status", "decided_by", "decided_at"])
                count += 1
        self.message_user(request, f"Approved {count} staff request(s).")
    approve_requests.short_description = "Approve selected requests"

    @transaction.atomic
    def reject_requests(self, request, queryset):
        changed = 0
        for req in queryset:
            if req.status != "rejected":
                req.status = "rejected"
                req.decided_by = request.user
                req.decided_at = timezone.now()
                req.save(update_fields=["status", "decided_by", "decided_at"])
                changed += 1
        self.message_user(request, f"Rejected {changed} staff request(s).")
    reject_requests.short_description = "Reject selected requests"

# =============== DIY Tutorials & Submissions ===============

@admin.register(DIYTutorial)
class DIYTutorialAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "is_active", "points_on_submit", "created_at")
    list_filter = ("is_active",)
    search_fields = ("title", "description")
    readonly_fields = ("created_at", "updated_at")

@admin.register(DIYDailyPool)
class DIYDailyPoolAdmin(admin.ModelAdmin):
    list_display = ("date",)
    filter_horizontal = ("tutorials",)
    search_fields = ("date",)

@admin.register(DIYDailySelection)
class DIYDailySelectionAdmin(admin.ModelAdmin):
    list_display = ("date", "tutorial", "pool", "selected_at")
    search_fields = ("date", "tutorial__title")
    readonly_fields = ("selected_at",)

@admin.register(DIYSubmission)
class DIYSubmissionAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "tutorial", "approved", "is_public", "points_awarded", "created_at")
    list_filter = ("approved", "is_public")
    search_fields = ("user__username", "user__email", "tutorial__title", "caption")
    readonly_fields = ("awarded_at", "created_at")
