
from django.urls import path
from . import views


urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    #path('users/', views.user_list, name='users'),
    #path('submissions/', views.submission_list, name='submissions'),
    #path('rewards/', views.reward_list, name='rewards'),
]
