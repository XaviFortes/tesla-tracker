# Stage 1: Builder
FROM python:3.11-slim as builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim

WORKDIR /app

# Create non-root user
RUN useradd -m -u 1000 teslauser

# Copy installed packages from builder
COPY --from=builder /root/.local /home/teslauser/.local

# Copy application code
COPY *.py .

# Prepare data directory for PVC
RUN mkdir /data && chown teslauser:teslauser /data

# Set environment
USER teslauser
ENV PATH=/home/teslauser/.local/bin:$PATH

# Expose Health Check Port
EXPOSE 8080

CMD ["python", "main.py"]