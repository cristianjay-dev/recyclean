from django.shortcuts import render, redirect, get_object_or_404
from .forms import DropOffPointForm

from dashboard.models import DropOffPoint

# Create your views here.
def dashboard(request):
    return render(request, 'dashboard.html')


def dropoff_list(request):
    dropoffs = DropOffPoint.objects.all()
    return render(request, 'dropoffs.html', {'dropoffs': dropoffs})

def dropoff_create(request):
    if request.method == 'POST':
        form = DropOffPointForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('dropoff_list')
    else:
        form = DropOffPointForm()
    return render(request, 'dropoff_form.html', {'form': form, 'form_title': 'Create Drop-Off Point'})

def dropoff_edit(request, pk):
    point = get_object_or_404(DropOffPoint, pk=pk)
    if request.method == 'POST':
        form = DropOffPointForm(request.POST, instance=point)
        if form.is_valid():
            form.save()
            return redirect('dropoff_list')
    else:
        form = DropOffPointForm(instance=point)
    return render(request, 'dropoff_form.html', {'form': form, 'form_title': 'Edit Drop-Off Point'})