
from django.urls import path
from . import views


urlpatterns = [
    path('', views.dashboard, name='dashboard'),
     path('dropoffs/', views.dropoff_list, name='dropoff_list'),
    path('dropoffs/add/', views.dropoff_create, name='dropoff_create'),
    path('dropoffs/edit/<int:pk>/', views.dropoff_edit, name='dropoff_edit'),
]
