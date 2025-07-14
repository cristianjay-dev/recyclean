from django.shortcuts import render, HttpResponse

from dashboard.models import RewardRequest, Submission, User

# Create your views here.
def dashboard(request):
    return render(request, 'dashboard.html')

