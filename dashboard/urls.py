from django.urls import path
from . import views

urlpatterns = [
    # ------------------ Dashboard ------------------
    path('', views.dashboard, name='dashboard'),

    # ------------------ Reward Requests ------------------
    path('rewards/', views.reward_requests_view, name='reward_requests'),
    path('rewards/approve/<int:pk>/', views.approve_reward, name='approve_reward'),

    # ------------------ Image Processing ------------------
    path('analyze/', views.analyze_image, name='analyze_image'),

    # ------------------ Quiz Management ------------------
    path('quiz/', views.quiz_dashboard, name='quiz_dashboard'),
    path('quiz/create-category/', views.create_category, name='create_category'),
    path('quiz/add-question/<int:category_id>/', views.add_question_to_category, name='add_question_to_category'),
    path('quiz/update-question/<int:quiz_id>/', views.update_question, name='update_question'),
    path('quiz/delete-question/<int:question_id>/', views.delete_question, name='delete_question'),
    path('quiz/delete-category/<int:category_id>/', views.delete_category, name='delete_category'),

    # ------------------ Staff & Drop-off Site Management ------------------
    path('dropoff-sites/', views.dropoff_sites_view, name='dropoff_sites_view'),
    path('staff-requests/', views.staff_requests_view, name='staff_requests_view'),
    path('approve-staff/<int:user_id>/', views.approve_staff, name='approve_staff'),
    path('api/staff-signup/', views.staff_signup_api, name='staff_signup_api'),
    path('api/staff-login/', views.staff_login_view, name='staff_login'),
    path('reject-staff/<int:user_id>/', views.reject_staff, name='reject_staff'),
    
    path('api/staff/metrics/<int:staff_id>/', views.staff_metrics, name='staff_metrics'),
    path('api/process-qr-submission/', views.process_qr_submission, name='process_qr_submission'),
    path('api/staff/<int:staff_id>/transactions/', views.staff_transaction_history, name='staff_transaction_history'),



]
