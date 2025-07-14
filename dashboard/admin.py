from django.contrib import admin
from .models import DropOffPoint, User, Submission, RewardRequest

@admin.register(DropOffPoint)
class DropOffPointAdmin(admin.ModelAdmin):
    list_display = ('name', 'address', 'latitude', 'longitude')
    search_fields = ('name', 'address')

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('name', 'total_points')

@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ('user', 'quantity', 'timestamp')

@admin.register(RewardRequest)
class RewardRequestAdmin(admin.ModelAdmin):
    list_display = ('user', 'mobile_number', 'load_amount', 'status')
    list_filter = ('status',)
