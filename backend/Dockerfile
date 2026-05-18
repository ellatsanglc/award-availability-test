FROM python:3.11-slim

# System dependencies required by Playwright/Chromium
RUN apt-get update && apt-get install -y \
    wget gnupg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python packages first (better layer caching)
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium and all its system dependencies
RUN playwright install chromium --with-deps

# Copy backend source code
COPY backend/ .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
