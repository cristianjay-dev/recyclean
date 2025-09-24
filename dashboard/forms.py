from django import forms
from django.contrib.auth.models import Group

from .models import DIYTutorial, DropOffSite, User


# -------------------------------
# DIY Tutorials (create / update)
# -------------------------------
class DIYTutorialForm(forms.ModelForm):
    class Meta:
        model = DIYTutorial
        fields = [
            "title",
            "description",
            "video_url",
            "duration_seconds",
            "thumbnail",
            "points_on_submit",
            "is_active",
        ]
        widgets = {
            "title": forms.TextInput(attrs={
                "class": "w-full p-2 border rounded",
                "id": "id_title",
            }),
            "description": forms.Textarea(attrs={
                "class": "w-full p-2 border rounded",
                "rows": 4,
                "id": "id_description",
            }),
            "video_url": forms.URLInput(attrs={
                "class": "w-full p-2 border rounded",
                "id": "id_video_url",
            }),
            "duration_seconds": forms.NumberInput(attrs={
                "class": "w-full p-2 border rounded",
                "min": 0,
                "id": "id_duration_seconds",
            }),
            "thumbnail": forms.ClearableFileInput(attrs={
                "class": "w-full p-2 border rounded",
                "id": "id_thumbnail",
            }),
            "points_on_submit": forms.NumberInput(attrs={
                "class": "w-full p-2 border rounded",
                "min": 0,
                "id": "id_points_on_submit",
            }),
            "is_active": forms.CheckboxInput(attrs={
                "class": "h-4 w-4",
                "id": "id_is_active",
            }),
        }

    def clean_points_on_submit(self):
        pts = self.cleaned_data.get("points_on_submit") or 0
        if pts < 0:
            raise forms.ValidationError("Points must be ≥ 0.")
        return pts

    def clean_duration_seconds(self):
        dur = self.cleaned_data.get("duration_seconds")
        if dur is not None and dur < 0:
            raise forms.ValidationError("Duration must be ≥ 0.")
        return dur


# ---------------------------------
# Drop-off Site (optional admin UI)
# ---------------------------------
class DropOffSiteForm(forms.ModelForm):
    class Meta:
        model = DropOffSite
        fields = ["barangay", "staff_members"]
        widgets = {
            "barangay": forms.Select(attrs={
                "class": "w-full p-2 border rounded",
                "id": "id_barangay",
            }),
            "staff_members": forms.SelectMultiple(attrs={
                "class": "w-full p-2 border rounded",
                "id": "id_staff_members",
                "size": 8,  # shows more rows; tweak as you like
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        staff_group = Group.objects.filter(name="staff").first()

        qs = User.objects.filter(is_active=True, is_approved=True)
        if staff_group:
            qs = qs.filter(groups=staff_group)
        else:
            qs = qs.filter(is_staff=True)

        self.fields["staff_members"].queryset = qs.order_by("username")
        self.fields["staff_members"].required = False
        self.fields["staff_members"].help_text = "Hold Ctrl/Cmd to select multiple staff."


# ---------------------------------
# Staff approval (simple checkbox)
# ---------------------------------
class StaffApprovalForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["is_approved"]
        widgets = {
            "is_approved": forms.CheckboxInput(attrs={"class": "h-4 w-4"}),
        }
