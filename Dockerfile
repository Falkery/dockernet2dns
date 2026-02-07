FROM python:3-slim

LABEL org.opencontainers.image.source=https://github.com/Falkery/dockernet2dns
LABEL org.opencontainers.image.description="Auto-update Technitium DNS records from Docker containers (supports IPvlan)"
LABEL org.opencontainers.image.licenses=MIT

# Install dependencies
RUN pip install --no-cache-dir docker requests

# Set working directory
WORKDIR /app

# Copy the script
COPY sync_dns.py .

# --- HEALTHCHECK ---
# Checks if the script has updated the health file within the last 120s
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import time, sys; sys.exit(0) if (time.time() - float(open('/tmp/healthy').read().strip())) < 120 else sys.exit(1)"

# Run unbuffered
CMD ["python", "-u", "sync_dns.py"]
