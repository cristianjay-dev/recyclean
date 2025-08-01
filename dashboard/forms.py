from django import forms
from .models import Quiz, QuizCategory, DropOffSite, User


class ManualQuizForm(forms.ModelForm):
    class Meta:
        model = Quiz
        fields = [
            'category', 'question', 'option_a', 'option_b',
            'option_c', 'option_d', 'correct_option', 'points', 'feedback'
        ]
        widgets = {
            'category': forms.Select(attrs={'class': 'w-full p-2 border rounded'}),
            'question': forms.Textarea(attrs={'class': 'w-full p-2 border rounded', 'rows': 3}),
            'option_a': forms.TextInput(attrs={'class': 'w-full p-2 border rounded'}),
            'option_b': forms.TextInput(attrs={'class': 'w-full p-2 border rounded'}),
            'option_c': forms.TextInput(attrs={'class': 'w-full p-2 border rounded'}),
            'option_d': forms.TextInput(attrs={'class': 'w-full p-2 border rounded'}),
            'correct_option': forms.Select(
                choices=[('a', 'A'), ('b', 'B'), ('c', 'C'), ('d', 'D')],
                attrs={'class': 'w-full p-2 border rounded'}
            ),
            'points': forms.NumberInput(attrs={'class': 'w-full p-2 border rounded'}),
            'feedback': forms.Textarea(attrs={'class': 'w-full p-2 border rounded', 'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        hide_category = kwargs.pop('hide_category', False)
        super().__init__(*args, **kwargs)
        if hide_category:
            self.fields['category'].widget = forms.HiddenInput()


class QuizCategoryForm(forms.ModelForm):
    class Meta:
        model = QuizCategory
        fields = ['category_name', 'description']
        widgets = {
            'category_name': forms.TextInput(attrs={'class': 'w-full p-2 border rounded'}),
            'description': forms.Textarea(attrs={'class': 'w-full p-2 border rounded', 'rows': 2}),
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
