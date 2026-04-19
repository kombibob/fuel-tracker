import os
import csv
import io
import requests
import sqlite3
import base64
import yfinance as yf
from flask import Flask, jsonify, render_template, request, Response
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, date
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

# --- Configuration ---
API_KEY    = os.environ.get("FUELCHECK_API_KEY", "YOUR_API_KEY_HERE")
API_SECRET = os.environ.get("FUELCHECK_API_SECRET", "YOUR_API_SECRET_HERE")
DB_PATH    = "/data/fuel.db"
NTFY_290   = os.environ.get("NTFY_290", "")
NTFY_300   = os.environ.get("NTFY_300", "")
NTFY_315   = os.environ.get("NTFY_315", "")

ALL_FUEL_TYPES = {
    "E10":  "Ethanol 10%",
    "U91":  "Unleaded 91",
    "P95":  "Premium 95",
    "P98":  "Premium 98",
    "DL":   "Diesel",
    "PDL":  "Premium Diesel",
    "LPG":  "LPG",
}

NEWCASTLE_LAT  = -32.9283
NEWCASTLE_LNG  = 151.7817
SEARCH_RADIUS  = 15

TOKEN_URL  = "https://api.onegov.nsw.gov.au/oauth/client_credential/accesstoken?grant_type=client_credentials"
NEARBY_URL = "https://api.onegov.nsw.gov.au/FuelPriceCheck/v1/fuel/prices/nearby"

_token_cache = {"token": None, "expires_at": 0}


def is_local_request():
    """Returns True if the request came directly from the local network,
    False if it came through Cloudflare tunnel."""
    return not request.headers.get("CF-Connecting-IP", "")


def get_token():
    now = datetime.now().timestamp()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    credentials = base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
    try:
        r = requests.get(
            TOKEN_URL,
            headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = now + int(data.get("expires_in", 43200))
        log.info("OAuth token refreshed")
        return _token_cache["token"]
    except Exception as e:
        log.error("Failed to get OAuth token: %s", e)
        return None


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fetched_at TEXT NOT NULL, suburb TEXT NOT NULL, station TEXT NOT NULL,
        address TEXT, fuel_type TEXT NOT NULL, price REAL NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS crude_oil (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL UNIQUE, brent_usd REAL, brent_aud REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    c.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('threshold_290','290')")
    c.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('threshold_300','300')")
    c.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('threshold_315','315')")
    c.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('fuel_types','DL,PDL')")
    conn.commit()
    conn.close()
    log.info("Database initialised at %s", DB_PATH)


def get_setting(key, default=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, str(value)))
    conn.commit()
    conn.close()


def get_all_settings():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT key, value FROM settings")
    rows = {r["key"]: r["value"] for r in c.fetchall()}
    conn.close()
    return rows


def get_active_fuel_types():
    fuel_str = get_setting("fuel_types", "DL,PDL")
    return [f.strip() for f in fuel_str.split(",") if f.strip()]


def save_prices(records):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executemany("INSERT INTO prices (fetched_at,suburb,station,address,fuel_type,price) VALUES (?,?,?,?,?,?)", records)
    conn.commit()
    conn.close()


def get_latest_prices():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT suburb,station,address,fuel_type,price,fetched_at FROM prices
        WHERE fetched_at=(SELECT MAX(fetched_at) FROM prices) ORDER BY price ASC""")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_price_history(days=30):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT DATE(fetched_at) as date, fuel_type,
        MIN(price) as min_price, ROUND(AVG(price),1) as avg_price,
        MAX(price) as max_price, COUNT(DISTINCT station) as station_count
        FROM prices WHERE fetched_at >= DATE('now',?)
        GROUP BY DATE(fetched_at),fuel_type ORDER BY date DESC""", (f"-{days} days",))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_crude_history(days=30):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT date, brent_usd, brent_aud FROM crude_oil
        WHERE date >= DATE('now',?) ORDER BY date DESC""", (f"-{days} days",))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_latest_crude():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT date, brent_usd, brent_aud FROM crude_oil ORDER BY date DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_prices_for_export():
    """Get all prices joined with crude oil data for CSV export."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT
            p.fetched_at,
            DATE(p.fetched_at) as date,
            p.suburb,
            p.station,
            p.address,
            p.fuel_type,
            p.price,
            co.brent_usd,
            co.brent_aud
        FROM prices p
        LEFT JOIN crude_oil co ON DATE(p.fetched_at) = co.date
        ORDER BY p.fetched_at DESC
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def fetch_and_store_crude():
    try:
        brent = yf.Ticker("BZ=F")
        brent_data = brent.history(period="2d")
        if brent_data.empty:
            log.warning("No Brent crude data returned")
            return
        brent_usd = round(float(brent_data["Close"].iloc[-1]), 2)
        audusd = yf.Ticker("AUDUSD=X")
        fx_data = audusd.history(period="2d")
        if not fx_data.empty:
            rate = float(fx_data["Close"].iloc[-1])
            brent_aud = round(brent_usd / rate, 2)
        else:
            brent_aud = None
            rate = None
        today = date.today().isoformat()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""INSERT INTO crude_oil (date, brent_usd, brent_aud)
            VALUES (?,?,?) ON CONFLICT(date) DO UPDATE SET
            brent_usd=excluded.brent_usd, brent_aud=excluded.brent_aud""",
            (today, brent_usd, brent_aud))
        conn.commit()
        conn.close()
        log.info("Brent crude: USD $%.2f | AUD $%.2f (rate: %.4f)", brent_usd, brent_aud or 0, rate or 0)
    except Exception as e:
        log.error("Crude oil fetch error: %s", e)


def derive_suburb(address):
    try:
        parts = address.split(",")
        if len(parts) >= 2:
            suburb_part = parts[-1].strip()
            return suburb_part.split(" NSW")[0].strip().title()
    except Exception:
        pass
    return ""


def fetch_prices_nearby(fuel_type, token):
    try:
        r = requests.post(
            NEARBY_URL,
            json={
                "fueltype":  fuel_type,
                "latitude":  NEWCASTLE_LAT,
                "longitude": NEWCASTLE_LNG,
                "radius":    SEARCH_RADIUS,
                "sortby":    "Price",
                "ascending": "true",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "transactionid": str(__import__("uuid").uuid4()),
                "requesttimestamp": datetime.now().strftime("%d/%m/%Y %H:%M:%S %p"),
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        stations_info = {s["code"]: s for s in data.get("stations", [])}
        results = []
        for p in data.get("prices", []):
            code    = p.get("stationcode")
            station = stations_info.get(code, {})
            address = station.get("address", "")
            results.append({
                "name":    station.get("name", f"Station {code}"),
                "address": address,
                "suburb":  derive_suburb(address),
                "price":   p.get("price"),
            })
        log.info("Fetched %d stations for %s within %dkm", len(results), fuel_type, SEARCH_RADIUS)
        return results
    except requests.RequestException as e:
        resp = e.response.text if hasattr(e, "response") and e.response is not None else "no response"
        log.error("FuelCheck nearby API error for %s: %s | %s", fuel_type, e, resp)
        return []


def fetch_and_store():
    log.info("Starting price fetch at %s", datetime.now().isoformat())
    token = get_token()
    if not token:
        log.error("Aborting fetch — no valid token")
        return

    fuel_types = get_active_fuel_types()
    log.info("Fetching fuel types: %s", fuel_types)

    fetched_at = datetime.now().isoformat(timespec="seconds")
    records    = []
    cheapest   = {}

    for fuel_type in fuel_types:
        stations = fetch_prices_nearby(fuel_type, token)
        for s in stations:
            try:
                price    = float(s.get("price", 0))
                name     = s.get("name", "Unknown")
                addr_str = s.get("address", "")
                suburb   = s.get("suburb", "").title().strip()
                records.append((fetched_at, suburb, name, addr_str, fuel_type, price))
                if fuel_type not in cheapest or price < cheapest[fuel_type]["price"]:
                    cheapest[fuel_type] = {"price": price, "station": name, "suburb": suburb}
            except (ValueError, TypeError):
                continue

    if records:
        save_prices(records)
        log.info("Stored %d price records", len(records))
    else:
        log.warning("No price records returned from API")

    t290 = float(get_setting("threshold_290", 290))
    t300 = float(get_setting("threshold_300", 300))
    t315 = float(get_setting("threshold_315", 315))

    for fuel, info in cheapest.items():
        label = ALL_FUEL_TYPES.get(fuel, fuel)
        msg = f"{label} {info['price']:.1f}c — {info['station']}, {info['suburb']}"
        if info["price"] <= t290 and NTFY_290:
            send_ntfy_alert(NTFY_290, msg)
        if info["price"] <= t300 and NTFY_300:
            send_ntfy_alert(NTFY_300, msg)
        if info["price"] <= t315 and NTFY_315:
            send_ntfy_alert(NTFY_315, msg)


def send_ntfy_alert(url, message):
    try:
        requests.post(url, data=message.encode("utf-8"),
            headers={"Title": "Fuel Price Alert", "Priority": "default", "Tags": "fuelpump"}, timeout=5)
        log.info("ntfy alert sent to %s: %s", url, message)
    except requests.RequestException as e:
        log.error("ntfy error: %s", e)


@app.route("/")
def index():
    is_local = is_local_request()
    return render_template("index.html", is_local=is_local)

@app.route("/api/prices/latest")
def api_latest():
    return jsonify(get_latest_prices())

@app.route("/api/prices/history")
def api_history():
    return jsonify(get_price_history(30))

@app.route("/api/crude/latest")
def api_crude_latest():
    return jsonify(get_latest_crude())

@app.route("/api/crude/history")
def api_crude_history():
    return jsonify(get_crude_history(30))

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(get_all_settings())

@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    if not is_local_request():
        return jsonify({"error": "Not available"}), 403
    data = request.json
    for key, value in data.items():
        if key.startswith("threshold_"):
            try:
                set_setting(key, float(value))
            except ValueError:
                return jsonify({"status": "error", "message": f"Invalid value for {key}"}), 400
        elif key == "fuel_types":
            valid = [f for f in value.split(",") if f.strip() in ALL_FUEL_TYPES]
            if valid:
                set_setting("fuel_types", ",".join(valid))
    return jsonify({"status": "ok"})

@app.route("/api/fuel-types")
def api_fuel_types():
    active = get_active_fuel_types()
    return jsonify({"all": ALL_FUEL_TYPES, "active": active})

@app.route("/api/export/csv")
def api_export_csv():
    if not is_local_request():
        return jsonify({"error": "Not available"}), 403
    rows = get_all_prices_for_export()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "fetched_at", "date", "suburb", "station", "address",
        "fuel_type", "price_cents_per_litre", "brent_usd", "brent_aud"
    ])
    for row in rows:
        writer.writerow([
            row["fetched_at"],
            row["date"],
            row["suburb"],
            row["station"],
            row["address"],
            row["fuel_type"],
            row["price"],
            row["brent_usd"] or "",
            row["brent_aud"] or "",
        ])
    output.seek(0)
    filename = f"fuel_prices_{date.today().isoformat()}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    fetch_and_store()
    fetch_and_store_crude()
    return jsonify({"status": "ok"})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(fetch_and_store, "cron", hour="7,17", minute=0)
    scheduler.add_job(fetch_and_store_crude, "cron", hour="7", minute=5)
    scheduler.start()
    log.info("Scheduler started — fuel 07:00 & 17:00, crude 07:05 daily")


if __name__ == "__main__":
    init_db()
    start_scheduler()
    fetch_and_store()
    fetch_and_store_crude()
    app.run(host="0.0.0.0", port=5000, debug=False)
