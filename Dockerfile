FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Railway sets PORT but our bot doesn't need an HTTP server.
# We expose nothing — Railway detects long-running processes fine.
CMD ["python", "bot.py"]
