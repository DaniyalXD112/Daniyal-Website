FROM python:3.11-slim

WORKDIR /app

# Copy application files
COPY . /app

# Cloud runtime environment defaults
ENV HOST=0.0.0.0
ENV PORT=10000
ENV GAMEVAULT_HOST=0.0.0.0
ENV GAMEVAULT_DB=playscape.sqlite3

EXPOSE 10000

CMD ["python", "server.py"]
