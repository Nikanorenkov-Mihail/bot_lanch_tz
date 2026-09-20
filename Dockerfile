FROM python:3.12-slim

WORKDIR /app

# Tesseract OCR + русский языковой пакет — для бесплатного распознавания меню
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Здесь живут orders.db, employees.json, today_menu.json — монтируется как volume,
# чтобы данные переживали пересборку/обновление образа.
VOLUME ["/app/data"]

CMD ["python", "bot.py"]
