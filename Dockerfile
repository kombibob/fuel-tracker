FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    flask \
    requests \
    apscheduler \
    yfinance

# Download NSW locality data at build time — stored in /app (not /data volume)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && curl -fsSL "https://raw.githubusercontent.com/matthewproctor/australianpostcodes/master/australian_postcodes.csv" \
       -o /tmp/postcodes_raw.csv \
    && python3 -c "import csv; rows = [{'locality': r['locality'].strip(), 'postcode': r['postcode'].strip(), 'lat': r['lat'], 'lng': r['long']} for r in csv.DictReader(open('/tmp/postcodes_raw.csv', encoding='utf-8')) if r.get('state','').strip().upper() == 'NSW' and r['lat'] and r['long']]; f = open('/app/nsw_localities.csv', 'w', newline=''); w = csv.DictWriter(f, fieldnames=['locality','postcode','lat','lng']); w.writeheader(); w.writerows(rows); f.close(); print(f'Written {len(rows)} NSW localities')" \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* /tmp/postcodes_raw.csv

COPY app.py .
COPY templates/ templates/

VOLUME ["/data"]

EXPOSE 5000

CMD ["python", "app.py"]
