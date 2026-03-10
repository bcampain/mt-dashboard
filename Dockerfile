# mt-dashboard
# Lightweight Python image serving the FastAPI backend + static SPA.
# All media-tools volume mounts are read-only from this container's perspective.

FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer cache-friendly)
COPY api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy API code
COPY api/ ./api/

# Copy SPA assets
COPY web/ ./web/

# Expose dashboard port (internal; compose maps this to the host)
EXPOSE 8080

# WEB_DIR is read by main.py to locate the SPA static files
ENV WEB_DIR=/app/web

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
