"""
Django settings for recyclean project.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Paths / env
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=Path(BASE_DIR) / ".env.local")

# --- Reloadly config ---
RELOADLY_ENV = os.getenv("RELOADLY_ENV", "sandbox")  # 'sandbox' or 'live'

# OAuth client credentials (from Reloadly portal)
RELOADLY_CLIENT_ID = os.getenv("RELOADLY_CLIENT_ID", "")
RELOADLY_CLIENT_SECRET = os.getenv("RELOADLY_CLIENT_SECRET", "")

# Webhook signing secret (from Reloadly webhook settings)
RELOADLY_WEBHOOK_SECRET = os.getenv("RELOADLY_WEBHOOK_SECRET", "")

# Bases (override via .env only if needed)
RELOADLY_AUTH_BASE = os.getenv("RELOADLY_AUTH_BASE", "https://auth.reloadly.com")
RELOADLY_TOPUPS_BASE = os.getenv(
    "RELOADLY_TOPUPS_BASE",
    "https://topups-sandbox.reloadly.com" if RELOADLY_ENV == "sandbox" else "https://topups.reloadly.com"
)

# OAuth audience must match the topups base
RELOADLY_AUDIENCE = os.getenv("RELOADLY_AUDIENCE", RELOADLY_TOPUPS_BASE)

# Reloadly requires a versioned Accept header
RELOADLY_ACCEPT_HEADER = os.getenv("RELOADLY_ACCEPT_HEADER", "application/com.reloadly.topups-v1+json")

# If your webhook URL is on a public domain, trust it for CSRF (optional)
# CSRF_TRUSTED_ORIGINS = ["https://localendpoint.com"]

# --- Django basics ---
SECRET_KEY = 'django-insecure-)ya+&5wj4tmvrxo4c+u&@f_jnub3*#t*wbx2dm23i9wz#=p$+z'  # move to .env for prod
DEBUG = True
ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    "dashboard",
    'rest_framework',
    'corsheaders',
]

CORS_ALLOW_ALL_ORIGINS = True
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'recyclean.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'recyclean.wsgi.application'

# Database
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get("DB_NAME", "capstone_db"),
        'USER': os.environ.get("DB_USER", "postgres"),
        'PASSWORD': os.environ.get("DB_PASSWORD", "Aspire5#073020"),  # move to .env for prod
        'HOST': os.environ.get("DB_HOST", "localhost"),
        'PORT': os.environ.get("DB_PORT", "5432"),
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# I18N / TZ
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Manila'   # local time
USE_I18N = True
USE_TZ = True

# Static
STATIC_URL = 'static/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
