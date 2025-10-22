# forms.py
from django import forms
from django.contrib.auth.models import Group

from .models import DIYTutorial, DropOffSite, User, PointsConfig

# Tailwind classes (kept DRY)
_BASE_INPUT = "mt-1 w-full rounded-lg border-gray-300 focus:border-blue-500 focus:ring-blue-500"
_FILE_INPUT = (
    "mt-1 w-full rounded-lg border-gray-300 focus:border-blue-500 focus:ring-blue-500 "
    "file:mr-3 file:rounded-md file:border-0 file:bg-gray-100 file:px-3 file:py-1.5 hover:file:bg-gray-200"
)
_CHECKBOX = "h-5 w-5 rounded text-blue-600 focus:ring-blue-500"

# -------------------------------
# DIY Tutorials (create / update)
# -------------------------------
class DIYTutorialForm(forms.ModelForm):
    title = forms.CharField(required=False)
    video_url = forms.URLField(required=False)
    duration_seconds = forms.IntegerField(min_value=0, required=False)
    thumbnail = forms.ImageField(required=False)
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            "class": _BASE_INPUT,
            "rows": 4,
            "id": "id_description",
            "placeholder": "Steps, tips… (you can use -, *, • for bullets)",
        }),
    )

    force_refresh_meta = forms.BooleanField(
        required=False,
        initial=False,
        label="Refresh metadata from YouTube",
        help_text="Fetch title, duration, and thumbnail/description from the video URL even if fields are already filled."
    )

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
                "class": _BASE_INPUT,
                "id": "id_title",
                "placeholder": "Short, clear title",
            }),
            "video_url": forms.URLInput(attrs={
                "class": _BASE_INPUT,
                "id": "id_video_url",
                "placeholder": "https://youtu.be/...",
            }),
            "duration_seconds": forms.NumberInput(attrs={
                "class": _BASE_INPUT,
                "min": 0,
                "id": "id_duration_seconds",
            }),
            "thumbnail": forms.ClearableFileInput(attrs={
                "class": _FILE_INPUT,
                "id": "id_thumbnail",
            }),
            "points_on_submit": forms.NumberInput(attrs={
                "class": _BASE_INPUT,
                "min": 0,
                "id": "id_points_on_submit",
            }),
            "is_active": forms.CheckboxInput(attrs={
                "class": _CHECKBOX,
                "id": "id_is_active",
            }),
        
        }
        help_texts = {
            "video_url": "Paste a YouTube link. Title, duration, and description can auto-fill.",
            "title": "Leave blank to auto-fill from YouTube.",
            "description": "Leave blank to auto-fill from YouTube (we’ll truncate to 4000 chars).",
            "duration_seconds": "Leave blank to auto-fill from YouTube.",
            "thumbnail": "Optional. If blank, we’ll use the YouTube thumbnail.",
        }

    # Optional safety: re-apply classes if overridden elsewhere
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].widget.attrs.setdefault("class", _BASE_INPUT)
        self.fields["description"].widget.attrs.setdefault("class", _BASE_INPUT)
        self.fields["video_url"].widget.attrs.setdefault("class", _BASE_INPUT)
        self.fields["duration_seconds"].widget.attrs.setdefault("class", _BASE_INPUT)
        self.fields["thumbnail"].widget.attrs.setdefault("class", _FILE_INPUT)
        self.fields["points_on_submit"].widget.attrs.setdefault("class", _BASE_INPUT)
        self.fields["is_active"].widget.attrs.setdefault("class", _CHECKBOX)
        # add class for the non-model checkbox
        self.fields["force_refresh_meta"].widget.attrs.setdefault("class", _CHECKBOX)

    def clean_title(self):
        return (self.cleaned_data.get("title") or "").strip()

    def clean_description(self):
        desc = (self.cleaned_data.get("description") or "").strip()
        return desc[:4000]

    def clean_video_url(self):
        url = (self.cleaned_data.get("video_url") or "").strip()
        return url or None

    def clean_points_on_submit(self):
        raw = self.cleaned_data.get("points_on_submit")
        if raw in (None, ""):
            return 0
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise forms.ValidationError("Points must be a whole number ≥ 0.")
        if v < 0:
            raise forms.ValidationError("Points must be ≥ 0.")
        return v

    def clean_duration_seconds(self):
        dur = self.cleaned_data.get("duration_seconds")
        if dur is None:
            return None
        dur = int(dur)
        if dur < 0:
            raise forms.ValidationError("Duration must be ≥ 0.")
        return dur


# -------------------------------------------------
# "Submission" admin page — bottle points assignment
# -------------------------------------------------
class PointsConfigForm(forms.ModelForm):
    class Meta:
        model = PointsConfig
        fields = ["small_bottle_points", "large_bottle_points"]
        labels = {
            "small_bottle_points": "Small bottle points",
            "large_bottle_points": "Large bottle points",
        }
        widgets = {
            "small_bottle_points": forms.NumberInput(attrs={
                "class": _BASE_INPUT,
                "min": 0,
                "id": "id_small_bottle_points",
                "step": 1,
            }),
            "large_bottle_points": forms.NumberInput(attrs={
                "class": _BASE_INPUT,
                "min": 0,
                "id": "id_large_bottle_points",
                "step": 1,
            }),
        }

    def clean_small_bottle_points(self):
        v = self.cleaned_data.get("small_bottle_points") or 0
        if v < 0:
            raise forms.ValidationError("Must be ≥ 0.")
        return v

    def clean_large_bottle_points(self):
        v = self.cleaned_data.get("large_bottle_points") or 0
        if v < 0:
            raise forms.ValidationError("Must be ≥ 0.")
        return v


# ---------------------------------
# Drop-off Site (shown on Submission admin)
# ---------------------------------
class DropOffSiteForm(forms.ModelForm):
    class Meta:
        model = DropOffSite
        fields = ["barangay", "staff_members"]
        widgets = {
            "barangay": forms.Select(attrs={
                "class": _BASE_INPUT,
                "id": "id_barangay",
            }),
            "staff_members": forms.SelectMultiple(attrs={
                "class": _BASE_INPUT,
                "id": "id_staff_members",
                "size": 8,
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
            "is_approved": forms.CheckboxInput(attrs={"class": _CHECKBOX}),
        }
