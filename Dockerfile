FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY *.html ./
EXPOSE 8080
ENV DATA_DIR=/data PANEL_HOST=0.0.0.0 PANEL_PORT=8080 COOKIE_SECURE=1 BEHIND_PROXY=1 \
    IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg PYTHONUNBUFFERED=1
CMD ["python", "-u", "panel.py"]
