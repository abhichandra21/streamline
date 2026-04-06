FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Non-root user for security
RUN useradd -m -u 1000 streamline \
    && chown -R streamline:streamline /app
USER streamline

# Gunicorn via the web entrypoint
ENV STREAMLINE_HOST=0.0.0.0
ENV STREAMLINE_PORT=5050

EXPOSE 5050

CMD ["gunicorn", \
     "--workers", "1", \
     "--bind", "0.0.0.0:5050", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "recommender.web:app"]
