# Recipe for the Python box (used by both api and worker).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv/backend

# System tools: Tesseract for OCR, libmagic for file-type detection
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng libmagic1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# Install Python libraries first (cached between builds)
COPY backend/requirements.txt /srv/backend/requirements.txt
RUN pip install -r /srv/backend/requirements.txt

# Copy the code (in development the folders are also mounted live)
COPY backend /srv/backend
COPY frontend /srv/frontend

# Run as a normal user, not root
RUN useradd --create-home appuser && chown -R appuser:appuser /srv
USER appuser

WORKDIR /srv/backend
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]