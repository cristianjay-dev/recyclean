from django.urls import path
from . import views

urlpatterns = [
    # ------------------ Dashboard ------------------
    path('', views.dashboard, name='dashboard'),
    path('rewards/', views.reward_requests_view, name='reward_requests'),

    # ------------------ Image Processing ------------------
    path('analyze/', views.analyze_image, name='analyze_image'),

    # ------------------ REVISED: Quiz Management (Admin) ------------------
    path('quiz/', views.quiz_dashboard, name='quiz_dashboard'),
    path('quiz/create/', views.create_quiz, name='create_quiz'),
    path('quiz/update/<int:quiz_id>/', views.update_question, name='update_question'),
    path('quiz/delete/<int:question_id>/', views.delete_question, name='delete_question'),

    # ------------------ Staff & Drop-off Site Management ------------------
    path('dropoff-sites/', views.dropoff_sites_view, name='dropoff_sites_view'),
    path('staff-management/', views.staff_management_view, name='staff_management_view'),
    path('approve-staff/<int:user_id>/', views.approve_staff, name='approve_staff'),
    path('reject-staff/<int:user_id>/', views.reject_staff, name='reject_staff'),
    path('delete-staff/<int:staff_id>/', views.delete_staff, name='delete_staff'),
    path('dropoff-sites/<int:site_id>/', views.dropoff_site_detail, name='dropoff_site_detail'),
    path('delete-dropoff-site/<int:site_id>/', views.delete_dropoff_site, name='delete_dropoff_site'),
    
    # ------------------ API (Staff App) ------------------
    path('api/staff-signup/', views.staff_signup_api, name='staff_signup_api'),
    path('api/staff-login/', views.staff_login_view, name='staff_login'),
    path('get-staff-transactions/<int:staff_id>/', views.get_staff_transactions, name='get_staff_transactions'),
    path('api/staff/metrics/<int:staff_id>/', views.staff_metrics, name='staff_metrics'),
    path('api/process-qr-submission/', views.process_qr_submission, name='process_qr_submission'),
    path('api/staff/<int:staff_id>/transactions/', views.staff_transaction_history, name='staff_transaction_history'),

    # ------------------ API (User App) ------------------
    path('api/user-signup/', views.resident_signup_api, name='resident_signup_api'),
    path('api/user-login/', views.resident_login_api, name='resident_login_api'),
    path('api/user/<int:user_id>/', views.get_user_details, name='get_user_details'),
    
    # Redeem rewards (Reloadly)
    path('api/redeem-reward/', views.redeem_reward_api, name='redeem_reward_api'),
    
    # ------------------ REVISED: Quiz API (User App) ------------------
    path('api/quizzes/status/<int:user_id>/', views.get_quiz_status, name='api_get_quiz_status'),
    path('api/quizzes/questions/<str:quiz_type>/<int:user_id>/', views.get_quiz_questions, name='api_get_quiz_questions'),
    path('api/quizzes/submit/', views.submit_quiz_answers, name='api_submit_quiz_answers'),

    # ------------------ WEBHOOKS ------------------
    path('webhooks/reloadly/', views.reloadly_webhook, name='reloadly_webhook'),
]
