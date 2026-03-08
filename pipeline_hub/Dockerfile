FROM python:3.12-slim

WORKDIR /app

# Install Kerberos system libraries (required by requests-kerberos / gssapi)
# krb5-multidev provides the krb5-config binary needed during pip install
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    pkg-config \
    python3-dev \
    libkrb5-dev \
    krb5-multidev \
    libgssapi-krb5-2 && \
    rm -rf /var/lib/apt/lists/*

# Verify krb5-config is available before pip install
RUN which krb5-config || (echo "ERROR: krb5-config not found in PATH" && exit 1)

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
