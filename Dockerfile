FROM python:3.11-slim

WORKDIR /app

# System deps needed by some packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Keeps Python output unbuffered so logs show in Railway immediately
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
