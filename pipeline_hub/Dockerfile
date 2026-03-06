FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Non-root user for security
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 9090

# Ensure Python output is not buffered (so logs appear in kubectl logs)
ENV PYTHONUNBUFFERED=1

# Use gunicorn for production
CMD ["gunicorn", "--bind", "0.0.0.0:9090", "--workers", "2", "--timeout", "120", "--access-logfile", "-", "app:app"]
