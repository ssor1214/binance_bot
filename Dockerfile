FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY run_forever.py .
COPY scripts ./scripts

# .env는 이미지에 포함하지 않고 런타임에 주입한다 (docker run --env-file .env ...)
# run_forever.py로 실행 — bot.main이 응답불능/크래시 상태가 되면 자동으로 감지해 재시작한다.
# bot.main을 직접 실행하면 이 자동복구 기능이 빠지므로 반드시 run_forever.py를 통해 띄울 것.
CMD ["python", "run_forever.py"]
