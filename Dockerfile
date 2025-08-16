# ---- base image
FROM python:3.11-slim

# ---- system deps (ffmpeg gives us ffmpeg + ffprobe)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# ---- python deps
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# ---- app files
COPY . .

# Railway provides $PORT
ENV PORT=8080
EXPOSE 8080

# Gunicorn server for Flask
CMD ["gunicorn", "-w", "2", "-k", "gthread", "-t", "120", "-b", "0.0.0.0:${PORT}", "app:app"]
