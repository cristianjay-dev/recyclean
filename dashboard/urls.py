from django.urls import path
from django.http import HttpResponse
from django.conf import settings
from django.conf.urls.static import static
from . import views

urlpatterns = [
    # ------------------ Dashboard (server-rendered) ------------------
    path("", views.dashboard, name="dashboard"),
    path("rewards/", views.reward_requests_view, name="reward_requests"),

    # ------------------ Image Processing (prototype) -----------------
    path("analyze/", views.analyze_image, name="analyze_image"),

    # ------------------ Drop-off Sites (server-rendered) -------------
    path("dropoff-sites/", views.dropoff_sites_view, name="dropoff_sites_view"),
    path("dropoff-sites/<int:site_id>/", views.dropoff_site_detail, name="dropoff_site_detail"),
    path("dropoff-sites/<int:site_id>/submissions/", views.submissions_by_dropoff_site, name="submissions_by_dropoff_site"),

    # CSV exports
    path("exports/submissions.csv", views.export_all_submissions_csv, name="export_all_submissions_csv"),
    path("dropoff-sites/<int:site_id>/export.csv", views.export_site_submissions_csv, name="export_site_submissions_csv"),

    # Delete site
    path("delete-dropoff-site/<int:site_id>/", views.delete_dropoff_site, name="delete_dropoff_site"),

    # ------------------ Staff Management (server-rendered) -----------
    path("staff-management/", views.staff_management_view, name="staff_management_view"),
    # urls.py
    path("admin/staff/<int:user_id>/approve/", views.approve_staff_json, name="approve_staff_json"),
    path("admin/staff/<int:user_id>/reject/",  views.reject_staff_json,  name="reject_staff_json"),


    # ------------------ Auth: Staff ---------------------------------
    path("api/auth/staff/signup/", views.StaffSignupView.as_view(), name="staff_signup"),
    path("api/auth/staff/login/", views.StaffLoginView.as_view(), name="staff_login"),
    path("api/auth/staff/approve/<int:user_id>/", views.ApproveStaffView.as_view(), name="approve_staff"),
    path("api/auth/staff/reject/<int:user_id>/", views.RejectStaffView.as_view(), name="reject_staff"),

    path("api/utils/username-available/", views.username_available, name="username_available"),
    path("api/user/<int:user_id>/history/", views.user_history, name="user_history"),


    # Barangays (support both with and without trailing slash + legacy alias)
    path("api/barangays/", views.list_barangays, name="list_barangays"),
    path("api/barangays", views.list_barangays, name="list_barangays_noslash"),
    path("api/geo/barangays/", views.list_barangays, name="list_barangays_legacy"),
    path("api/geo/barangays", views.list_barangays, name="list_barangays_legacy_noslash"),

    # ------------------ Auth: Resident -------------------------------
    path("api/auth/resident/signup/", views.ResidentSignupView.as_view(), name="resident_signup"),
    path("api/auth/resident/login/", views.ResidentLoginView.as_view(), name="resident_login"),
    path("api/me/", views.MeView.as_view(), name="me"),
    path("api/auth/change-password/", views.ChangePasswordView.as_view(), name="change_password"),

    # ------------------ Submissions: intake → QR → claim -------------
    path("api/submissions/intake/", views.SubmissionIntakeView.as_view(), name="submission_intake"),
    path("api/submissions/<int:submission_id>/qr.png", views.SubmissionQRView.as_view(), name="submission_qr"),
    path("api/submissions/claim/", views.SubmissionClaimView.as_view(), name="submission_claim"),

    # ------------------ Staff activity -------------------------------
    path("api/staff/<int:staff_id>/transactions/", views.staff_transaction_history, name="staff_transaction_history"),

    # ------------------ User dashboard data --------------------------
    path("api/user/<int:user_id>/", views.get_user_details, name="get_user_details"),

    # ------------------ Rewards (Reloadly) ---------------------------
    path("api/rewards/redeem/", views.RedeemRewardView.as_view(), name="redeem_reward"),
    path("webhooks/reloadly/", views.reloadly_webhook, name="reloadly_webhook"),

    path("api/utils/normalize-phone/", views.normalize_phone_ph, name="normalize_phone_ph"),

    # ------------------ DIY Tutorials (API) --------------------------
    # Support with/without slash + back-compat alias
    path("api/diy/daily/", views.api_diy_daily, name="api_diy_daily"),
    path("api/diy/daily", views.api_diy_daily, name="api_diy_daily_noslash"),
    path("api/diy/daily/alias/", views.diy_daily, name="diy_daily"),
    path("api/diy/daily/alias", views.diy_daily, name="diy_daily_noslash"),

    path("api/diy/feature/", views.diy_feature_today, name="diy_feature_today"),
    path("api/diy/create/", views.diy_create_tutorial, name="diy_create_tutorial"),
    path("api/diy/<int:tutorial_id>/update/", views.diy_update_tutorial, name="diy_update_tutorial"),
    path("api/diy/<int:tutorial_id>/delete/", views.diy_delete_tutorial, name="diy_delete_tutorial"),
    path("api/diy/submit/", views.DIYSubmitView.as_view(), name="diy_submit"),

    # ------------------ DIY Tutorials (server-rendered) --------------
    path("diy/", views.diy_dashboard, name="diy_dashboard"),

    # ------------------ Legacy aliases (optional) --------------------
    path("api/staff-signup/", views.StaffSignupView.as_view(), name="legacy_staff_signup"),
    path("api/staff-login/", views.StaffLoginView.as_view(), name="legacy_staff_login"),
    path("api/user-signup/", views.ResidentSignupView.as_view(), name="legacy_resident_signup"),
    path("api/user-login/", views.ResidentLoginView.as_view(), name="legacy_resident_login"),
    path("api/redeem-reward/", views.RedeemRewardView.as_view(), name="legacy_redeem_reward"),

    # Simple health check (useful for connectivity tests)
    path("health/", lambda r: HttpResponse("ok"), name="health"),
]