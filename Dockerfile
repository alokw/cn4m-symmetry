FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SOURCE_DIR=/data/source \
    TARGET_DIR=/data/target \
    SCAN_INTERVAL=60 \
    LINK_MODE=hard \
    WEB_PORT=2648

EXPOSE 2648

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY symmetry.py webui.py ./

ENTRYPOINT ["python", "-u", "/app/symmetry.py"]
