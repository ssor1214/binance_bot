FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# .env는 이미지에 포함하지 않고 런타임에 주입한다 (docker run --env-file .env ...)
CMD ["python", "-m", "bot.main"]
