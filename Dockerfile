FROM python:3.11-slim

# 1) ffmpeg (needed by your app)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# 2) app deps
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3) app code
COPY . .

ENV PYTHONUNBUFFERED=1
ENV PORT=8080
EXPOSE 8080

# 4) run gunicorn on Railway's assigned $PORT
CMD gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 600 app:app

