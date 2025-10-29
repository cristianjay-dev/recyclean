FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Manila \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    libpq-dev \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    libgfortran5 \
    libopenblas0 \
    liblapack3 \
    libjpeg62-turbo \
    zlib1g \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN mkdir -p /tmp/matplotlib && chmod 777 /tmp/matplotlib

COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
 && pip install --index-url https://download.pytorch.org/whl/cpu -r requirements.txt

COPY . .

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["gunicorn","recyclean.wsgi:application","--bind","0.0.0.0:8000","--workers","3","--timeout","180"]
