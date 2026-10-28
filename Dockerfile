FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Create non-root user
RUN addgroup --system app && adduser --system --ingroup app app

# Install dependencies (locked) into the project venv
COPY pyproject.toml uv.lock ./
RUN UV_LINK_MODE=copy uv sync --frozen --no-dev

# Copy application code
COPY . .
RUN chown -R app:app /app

USER app

EXPOSE 8000

# Single worker only — APScheduler runs inside the app process.
# Multiple workers would spawn duplicate schedulers.
# --proxy-headers trusts X-Forwarded-For from the reverse proxy for correct IP tracking.
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
