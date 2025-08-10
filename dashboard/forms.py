from django import forms
from .models import Quiz, DropOffSite, User


class ManualQuizForm(forms.ModelForm):
    # We define the choices here to use them in the template script.
    QUESTION_TYPE_CHOICES = [
        ('', '---------'), # Add a blank choice for the default state
        ('multiple_choice', 'Multiple Choice'),
        ('true_false', 'True/False'),
    ]
    
    # Explicitly define the question_type field to control its widget and choices.
    question_type = forms.ChoiceField(
        choices=QUESTION_TYPE_CHOICES,
        widget=forms.Select(attrs={
            'class': 'w-full p-2 border rounded', 
            'id': 'id_question_type' # Add an ID for easy JavaScript targeting
        })
    )

    class Meta:
        model = Quiz
        fields = [
            'question_type', 'question', 'option_a', 'option_b',
            'option_c', 'option_d', 'correct_option', 'points'
        ]
        widgets = {
            'question': forms.Textarea(attrs={'class': 'w-full p-2 border rounded', 'rows': 3}),
            
            # These options are for 'multiple_choice' and will be hidden/shown by JavaScript.
            # We add a class to them for easier selection in JS.
            'option_a': forms.TextInput(attrs={'class': 'w-full p-2 border rounded mc-option'}),
            'option_b': forms.TextInput(attrs={'class': 'w-full p-2 border rounded mc-option'}),
            'option_c': forms.TextInput(attrs={'class': 'w-full p-2 border rounded mc-option'}),
            'option_d': forms.TextInput(attrs={'class': 'w-full p-2 border rounded mc-option'}),
            
            # The options for this dropdown will be populated dynamically by JavaScript.
            'correct_option': forms.Select(attrs={
                'class': 'w-full p-2 border rounded', 
                'id': 'id_correct_option' # Add an ID for easy targeting
            }),
            
            'points': forms.NumberInput(attrs={'class': 'w-full p-2 border rounded'}),
        }


class DropOffSiteForm(forms.ModelForm):
    class Meta:
        model = DropOffSite
        fields = ['barangay', 'assigned_staff']
        widgets = {
            'barangay': forms.TextInput(attrs={'class': 'w-full p-2 border rounded'}),
            'assigned_staff': forms.Select(attrs={'class': 'w-full p-2 border rounded'})
        }


class StaffApprovalForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['is_approved']
