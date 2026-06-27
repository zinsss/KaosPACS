FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config
COPY tools ./tools

EXPOSE 105
EXPOSE 8055

CMD ["python", "-m", "app.main"]
