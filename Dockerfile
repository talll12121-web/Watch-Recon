FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8000
CMD gunicorn -w 1 --threads 8 -b 0.0.0.0:${PORT:-8000} app:app
