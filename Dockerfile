FROM python:3.11-slim

WORKDIR /app

# Copy project files including SQLite catalog
COPY . /app

# Set environment
ENV PORT=10000
ENV HOST=0.0.0.0
ENV RAWG_API_KEY=4b4c93a0cb2e4cd7ab734df6825760b8

EXPOSE 10000

CMD ["python", "server.py"]
