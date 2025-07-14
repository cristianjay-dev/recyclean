# dashboard/forms.py
from django import forms
from .models import DropOffPoint

class DropOffPointForm(forms.ModelForm):
    class Meta:
        model = DropOffPoint
        fields = ['name', 'address', 'latitude', 'longitude']
        widgets = {
             'name': forms.TextInput(attrs={'class': 'w-full p-2 border rounded', 'placeholder': 'Drop-off Point Name'}),
            'address': forms.Textarea(attrs={'class': 'w-full p-2 border rounded', 'placeholder': 'Full Address'}),
            'latitude': forms.NumberInput(attrs={'class': 'w-full p-2 border rounded', 'placeholder': 'Latitude'}),
            'longitude': forms.NumberInput(attrs={'class': 'w-full p-2 border rounded', 'placeholder': 'Longitude'}),

        }
