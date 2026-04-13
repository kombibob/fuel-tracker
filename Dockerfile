FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    flask \
    requests \
    apscheduler \
    yfinance

COPY app.py .
COPY templates/ templates/

VOLUME ["/data"]

EXPOSE 5000

CMD ["python", "app.py"]
