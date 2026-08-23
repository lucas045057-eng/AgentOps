FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium xvfb fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium

COPY . .
# Keep sensitive workbook files out of the image when an internal scripts
# directory is present. External daily scripts are mounted read-only at runtime.
RUN if [ -d /app/scripts ]; then find /app/scripts -name "*.xlsx" -type f -delete; fi

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
