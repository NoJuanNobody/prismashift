FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml /app/
COPY src/ /app/src/
COPY prismashift-admin /usr/local/bin/prismashift-admin
RUN chmod +x /usr/local/bin/prismashift-admin

# Install python dependencies
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    watchdog \
    pillow \
    pydantic \
    pydantic-settings \
    python-multipart

# Default storage directories
ENV PRISMASHIFT_INBOX_DIR=/data/inbox
ENV PRISMASHIFT_ORGANIZED_DIR=/data/sorted
ENV PRISMASHIFT_DB_PATH=/data/prismashift.db
ENV PRISMASHIFT_HOST=0.0.0.0
ENV PRISMASHIFT_PORT=8000

VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
