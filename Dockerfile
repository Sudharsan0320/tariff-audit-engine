FROM python:3.9-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY docs/ docs/

EXPOSE 8000

# Inside the container the API must bind 0.0.0.0; compose publishes it on the host's 127.0.0.1 only (E5).
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
