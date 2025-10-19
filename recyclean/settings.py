"""
Django settings for recyclean project.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# Paths / env
# -----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=BASE_DIR / ".env.local")

# -----------------------------------------------------------------------------
# Core security & app config
# -----------------------------------------------------------------------------
# REQUIRED before first migration (custom user model)
AUTH_USER_MODEL = "dashboard.User"

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = [h for h in os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",") if h]

# -----------------------------------------------------------------------------
# Installed apps / middleware
# -----------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "dashboard",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
]

# --- Admin app auth flow (login-first) ---
LOGIN_URL = "login"                 # send anonymous users to /login/
LOGIN_REDIRECT_URL = "dashboard" # after successful login
LOGOUT_REDIRECT_URL = "login"       # after logout
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Session quality-of-life (optional)
DEFAULT_FROM_EMAIL = "Recyclean <no-reply@localhost>"
PASSWORD_RESET_TIMEOUT = 60 * 60  # 1 hour, optional
SESSION_SAVE_EVERY_REQUEST = True


MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",  # keep first
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "recyclean.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "recyclean.wsgi.application"

# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME", "capstone_db"),
        "USER": os.getenv("DB_USER", "postgres"),   
        "PASSWORD": os.getenv("DB_PASSWORD", ""),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5432"),
    }
}

# -----------------------------------------------------------------------------
# Auth / DRF
# -----------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

REST_FRAMEWORK = {
    # Tighten per-view; you can switch to Token/JWT later for mobile
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.AllowAny",),
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "10/min",   # unauthenticated (covers login endpoints)
        "user": "60/min",
    },
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ),
}

# -----------------------------------------------------------------------------
# I18N / TZ
# -----------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Manila"
USE_I18N = True
USE_TZ = True

# -----------------------------------------------------------------------------
# Static & media
# -----------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"   # for collectstatic in prod

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# -----------------------------------------------------------------------------
# CORS / CSRF
# -----------------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = os.getenv("CORS_ALLOW_ALL", "true").lower() == "true"
# For production, set CORS_ALLOW_ALL=false and provide explicit origins:
# CORS_ALLOWED_ORIGINS = [o for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o]
# CSRF_TRUSTED_ORIGINS = [u for u in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if u]

# -----------------------------------------------------------------------------
# Reloadly config (from your original)
# -----------------------------------------------------------------------------
RELOADLY_ENV = os.getenv("RELOADLY_ENV", "sandbox")  # 'sandbox' or 'live'
RELOADLY_CLIENT_ID = os.getenv("RELOADLY_CLIENT_ID", "")
RELOADLY_CLIENT_SECRET = os.getenv("RELOADLY_CLIENT_SECRET", "")
RELOADLY_WEBHOOK_SECRET = os.getenv("RELOADLY_WEBHOOK_SECRET", "")
RELOADLY_AUTH_BASE = os.getenv("RELOADLY_AUTH_BASE", "https://auth.reloadly.com")
RELOADLY_TOPUPS_BASE = os.getenv(
    "RELOADLY_TOPUPS_BASE",
    "https://topups-sandbox.reloadly.com" if RELOADLY_ENV == "sandbox" else "https://topups.reloadly.com",
)
RELOADLY_AUDIENCE = os.getenv("RELOADLY_AUDIENCE", RELOADLY_TOPUPS_BASE)
RELOADLY_ACCEPT_HEADER = os.getenv("RELOADLY_ACCEPT_HEADER", "application/com.reloadly.topups-v1+json")

# -----------------------------------------------------------------------------
# Rewards / points conversion + helpers
# -----------------------------------------------------------------------------
# e.g. 1 PHP = 10 points  → ₱10 costs 100 points
POINTS_PER_PHP = int(os.getenv("POINTS_PER_PHP", "10"))

# Country/telco defaults used by rewards flow
RELOADLY_COUNTRY_CODE = os.getenv("RELOADLY_COUNTRY_CODE", "PH")

# Optional admin/shared-key bypass used by some staff-only endpoints (safe to leave blank)
ADMIN_SHARED_KEY = os.getenv("ADMIN_SHARED_KEY", "")

# Optional: allow bypassing staff checks in dev; keep False in prod
DIY_ADMIN_BYPASS = os.getenv("DIY_ADMIN_BYPASS", "false").lower() == "true"


# -----------------------------------------------------------------------------
# Optional extra security headers for production (HTTPS)
# -----------------------------------------------------------------------------
# SECURE_SSL_REDIRECT = True
# SESSION_COOKIE_SECURE = True
# CSRF_COOKIE_SECURE = True
# SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# DIY daily selection behavior (optionally configure via .env)
DIY_DAILY_STRATEGY = os.getenv("DIY_DAILY_STRATEGY", "round_robin")
DIY_NO_REPEAT_DAYS = int(os.getenv("DIY_NO_REPEAT_DAYS", "5"))
DIY_ROTATION_SALT   = int(os.getenv("DIY_ROTATION_SALT", "0"))

# NEW: how many tutorials to show per day
DIY_DAILY_COUNT = int(os.getenv("DIY_DAILY_COUNT", "3"))