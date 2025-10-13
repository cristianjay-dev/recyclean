# recyclean/urls.py
from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.conf.urls.static import static

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("dashboard.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    if getattr(settings, "STATIC_ROOT", None):
        urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)