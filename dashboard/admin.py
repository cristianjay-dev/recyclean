from django.conf import settings
from django.contrib import admin
from .models import User, Submission, RewardRequest, DropOffSite
import requests

# ------------------------
# USER ADMIN (Staff/Resident)
# ------------------------
@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('name', 'mobile_number', 'user_type', 'barangay', 'account_status', 'is_approved')
    list_filter = ('user_type', 'account_status', 'is_approved')
    search_fields = ('name', 'mobile_number')

    actions = ['approve_selected_staff']

    def approve_selected_staff(self, request, queryset):
        updated = queryset.filter(user_type='staff', is_approved=False).update(is_approved=True)
        self.message_user(request, f"{updated} staff accounts have been approved.")
    approve_selected_staff.short_description = "Approve selected staff accounts"

# ------------------------
# DROP-OFF SITE ADMIN
# ------------------------
@admin.register(DropOffSite)
class DropOffSiteAdmin(admin.ModelAdmin):
    list_display = ('barangay', 'assigned_staff')
    search_fields = ('barangay', 'assigned_staff__name')
    list_filter = ('barangay',)

# ------------------------
# SUBMISSION ADMIN
# ------------------------
@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ('user', 'staff', 'volume_type', 'quantity', 'points_awarded', 'date_submitted')
    list_filter = ('processing_mode', 'volume_type')
    search_fields = ('user__name', 'staff__name')

# ------------------------
# PAYREX AUTOMATED LOAD SENDER
# ------------------------
def approve_and_send_load(modeladmin, request, queryset):
    for req in queryset.filter(status='pending'):
        success = simulate_send_load(req.mobile_number, req.amount)
        if success:
            req.status = 'approved'
            req.save()
approve_and_send_load.short_description = "Approve and send load to selected requests"

def simulate_send_load(mobile_number, amount):
    url = "https://api.payrex.ph/send-load"  # Simulated or real endpoint
    headers = {
        "Authorization": f"Bearer {settings.PAYREX_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "mobile_number": mobile_number,
        "amount": float(amount),
        "telco": "AUTO",
    }
    try:
        response = requests.post(url, json=data, headers=headers)
        return response.status_code == 200
    except Exception as e:
        print("Error sending load:", e)
        return False

# ------------------------
# REWARD REQUEST ADMIN
# ------------------------
@admin.register(RewardRequest)
class RewardRequestAdmin(admin.ModelAdmin):
    list_display = ('user', 'mobile_number', 'amount', 'status', 'date_requested')
    list_filter = ('status',)
    search_fields = ('user__name', 'mobile_number')
    actions = [approve_and_send_load]
