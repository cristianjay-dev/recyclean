"""
Django settings for recyclean project.
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from celery.schedules import crontab

# -----------------------------------------------------------------------------
# Paths / env
# -----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent

# Prefer .env (prod) and fall back to .env.local (dev)
loaded = load_dotenv(dotenv_path=BASE_DIR / ".env", override= True)
if not loaded:
    load_dotenv(dotenv_path=BASE_DIR / ".env.local" , override= True)

ULTRA_DIR = BASE_DIR / ".ultralytics"
ULTRA_DIR.mkdir(exist_ok=True)
os.environ.setdefault("YOLO_CONFIG_DIR", str(ULTRA_DIR))

# -----------------------------------------------------------------------------
# Core security & app config
# -----------------------------------------------------------------------------
AUTH_USER_MODEL = "dashboard.User"

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() == "true"

# Comma-separated list => list[str] with trimming
ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]

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
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Session quality-of-life (optional)
DEFAULT_FROM_EMAIL = "Recyclean <no-reply@localhost>"
PASSWORD_RESET_TIMEOUT = 60 * 60  # 1 hour
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
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.AllowAny",),
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "10/min",
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
CORS_ALLOW_ALL_ORIGINS = os.getenv("CORS_ALLOW_ALL", "false").lower() == "true"
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
]
CSRF_TRUSTED_ORIGINS = [
    u.strip() for u in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if u.strip()
]

# -----------------------------------------------------------------------------
# Security for HTTPS behind Caddy / proxy
# (all controlled via .env so dev stays easy)
# -----------------------------------------------------------------------------
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "false").lower() == "true"
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
CSRF_COOKIE_SECURE = os.getenv("CSRF_COOKIE_SECURE", "false").lower() == "true"

# SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https (in .env)
_proxy_hdr = os.getenv("SECURE_PROXY_SSL_HEADER")
if _proxy_hdr:
    try:
        h, v = _proxy_hdr.split(",", 1)
        SECURE_PROXY_SSL_HEADER = (h.strip(), v.strip())
    except ValueError:
        pass  # ignore malformed env; keep default

# -----------------------------------------------------------------------------
# Reloadly config
# -----------------------------------------------------------------------------
RELOADLY_ENV = os.getenv("RELOADLY_ENV", "sandbox")
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
POINTS_PER_PHP = int(os.getenv("POINTS_PER_PHP", "10"))
RELOADLY_COUNTRY_CODE = os.getenv("RELOADLY_COUNTRY_CODE", "PH")
ADMIN_SHARED_KEY = os.getenv("ADMIN_SHARED_KEY", "")
DIY_ADMIN_BYPASS = os.getenv("DIY_ADMIN_BYPASS", "false").lower() == "true"

# -----------------------------------------------------------------------------
# DIY rotation (weekly 3 videos)
# -----------------------------------------------------------------------------
DIY_PERIOD = os.getenv("DIY_PERIOD", "week")
DIY_DAILY_COUNT = int(os.getenv("DIY_DAILY_COUNT", "3"))
DIY_NO_REPEAT_WEEKS = int(os.getenv("DIY_NO_REPEAT_WEEKS", "4"))
DIY_WEEK_START = int(os.getenv("DIY_WEEK_START", "0"))
DIY_ROTATION_SALT = int(os.getenv("DIY_ROTATION_SALT", "0"))
DIY_NO_CONSECUTIVE_WEEKS = os.getenv("DIY_NO_CONSECUTIVE_WEEKS", "true").lower() == "true"
DIY_SOFT_NO_REPEAT_WHEN_POOL_SMALL = os.getenv("DIY_SOFT_NO_REPEAT_WHEN_POOL_SMALL", "true").lower() == "true"

DIY_IMAGE_RETENTION_DAYS = int(os.getenv("DIY_IMAGE_RETENTION_DAYS", "365"))
DIY_IMAGE_CLEANUP_BATCH = int(os.getenv("DIY_IMAGE_CLEANUP_BATCH", "500"))

# ---- YOLO segmentation config ----
YOLO_SEG_WEIGHTS = Path(
    os.getenv(
        "YOLO_SEG_WEIGHTS",
        str(BASE_DIR / "ml" / "yolo" / "bottle" / "best.pt"),
    )
)

if not YOLO_SEG_WEIGHTS.exists():
    import warnings
    warnings.warn(f"YOLO weights not found at {YOLO_SEG_WEIGHTS}")

# Only fail fast in development
if DEBUG:
    assert YOLO_SEG_WEIGHTS.exists(), f"Missing weights at {YOLO_SEG_WEIGHTS}"

YOLO_DEVICE = "cpu"      # switch to "0" for CUDA:0 if available
YOLO_CONF = 0.68
YOLO_IOU = 0.70
YOLO_MAX_DET = 100
YOLO_IMG_SIZE = 896
SEG_CLASS_NAMES = ["small_bottle", "large_bottle"]

DEDUP_SAMECLASS_IOU = 0.80
AREA_PROMOTE_LARGE_FRAC = 0.12

# -----------------------------------------------------------------------------
# Celery / Redis
# -----------------------------------------------------------------------------
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1")
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = False  # we already use local TZ in Django

CELERY_BEAT_SCHEDULE = {
    "cleanup-diy-images-yearly": {
        "task": "dashboard.tasks.cleanup_diy_images_task",
        "schedule": crontab(minute=15, hour=3, day_of_month="1", month_of_year="1"),
        "args": (),
    },
}
