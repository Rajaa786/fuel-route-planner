FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir uv==0.11.26

WORKDIR /app

# Dependencies first: this layer is rebuilt only when the lockfile changes.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

COPY . .

# Bake the gazetteer and fuel stations into the image, so a container is ready
# to serve as soon as it starts. The key below is used for this build step only.
RUN DJANGO_SECRET_KEY=build-time-only python manage.py migrate --noinput \
    && DJANGO_SECRET_KEY=build-time-only python manage.py bootstrap_data

RUN useradd --system --no-create-home app && chown -R app /app
USER app

EXPOSE 8000
# Requires DJANGO_SECRET_KEY (and DJANGO_ALLOWED_HOSTS for non-local hosts) at run time.
CMD ["gunicorn", "config.wsgi"]
