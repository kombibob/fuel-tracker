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
from bs4 import BeautifulSoup

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

# Newcastle home base — used for scheduled fetches and storing history
NEWCASTLE_LAT  = -32.9283
NEWCASTLE_LNG  = 151.7817
SEARCH_RADIUS  = 15

VALID_RADII = [5, 10, 15, 20, 25]

TOKEN_URL  = "https://api.onegov.nsw.gov.au/oauth/client_credential/accesstoken?grant_type=client_credentials"
NEARBY_URL = "https://api.onegov.nsw.gov.au/FuelPriceCheck/v1/fuel/prices/nearby"

# Path to the bundled NSW locality CSV (added to container via Dockerfile)
LOCALITY_CSV = "/app/nsw_localities.csv"

_token_cache = {"token": None, "expires_at": 0}

# In-memory locality index: built once on startup
# Structure: list of dicts {suburb, postcode, lat, lng}
_localities = []


def load_localities():
    """Load NSW locality data from bundled CSV into memory."""
    global _localities
    if not os.path.exists(LOCALITY_CSV):
        log.warning("Locality CSV not found at %s", LOCALITY_CSV)
        return
    with open(LOCALITY_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                _localities.append({
                    "suburb":   row["locality"].strip().upper(),
                    "postcode": row["postcode"].strip(),
                    "lat":      float(row["lat"]),
                    "lng":      float(row["lng"]),
                })
            except (KeyError, ValueError):
                continue
    log.info("Loaded %d NSW localities", len(_localities))


def search_locality(query):
    """
    Search localities by postcode or suburb name.
    Returns a list of matches: [{suburb, postcode, lat, lng}, ...]
    """
    query = query.strip()
    if not query:
        return []

    results = []
    if query.isdigit():
        # Postcode lookup
        results = [l for l in _localities if l["postcode"] == query]
    else:
        # Suburb name lookup — prefix match first, then contains
        q = query.upper()
        results = [l for l in _localities if l["suburb"].startswith(q)]
        if not results:
            results = [l for l in _localities if q in l["suburb"]]

    # Deduplicate by suburb+postcode, keep first match
    seen = set()
    deduped = []
    for r in results:
        key = (r["suburb"], r["postcode"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    return deduped[:10]  # cap at 10 suggestions


def is_local_request():
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
    c.execute("""CREATE TABLE IF NOT EXISTS last_alerted_prices (fuel_type TEXT PRIMARY KEY, price REAL NOT NULL, alerted_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS terminal_gate_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL, location TEXT NOT NULL,
        fuel_type TEXT NOT NULL, price REAL NOT NULL,
        UNIQUE(date, location, fuel_type))""")
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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT p.fetched_at, DATE(p.fetched_at) as date, p.suburb, p.station,
               p.address, p.fuel_type, p.price, co.brent_usd, co.brent_aud
        FROM prices p
        LEFT JOIN crude_oil co ON DATE(p.fetched_at) = co.date
        ORDER BY p.fetched_at DESC
    """)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def fetch_and_store_tgp():
    """Scrape Mobil terminal gate prices and store Newcastle data."""
    try:
        r = requests.get(
            "https://www.mobil.com.au/en-au/commercial-fuels/terminal-gate",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if not table:
            log.warning("TGP: could not find pricing table on Mobil page")
            return
        today = date.today().isoformat()
        fuel_map = {
            0: "E10", 1: "U91", 2: "P95", 3: "P98", 4: "DL"
        }
        records = []
        for row in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in row.find_all(["td","th"])]
            # Look for the Newcastle row specifically
            if not cells:
                continue
            if "Newcastle" in cells:
                idx = cells.index("Newcastle")
                price_cells = cells[idx+1:]
                for i, fuel_code in fuel_map.items():
                    try:
                        val = price_cells[i] if len(price_cells) > i else ""
                        if val and val != "N/A":
                            records.append((today, "Newcastle", fuel_code, float(val)))
                    except (ValueError, IndexError):
                        continue
                break
        if records:
            conn = sqlite3.connect(DB_PATH)
            conn.executemany("""INSERT INTO terminal_gate_prices
                (date, location, fuel_type, price) VALUES (?,?,?,?)
                ON CONFLICT(date, location, fuel_type) DO UPDATE SET
                price=excluded.price""", records)
            conn.commit()
            conn.close()
            log.info("TGP: stored %d Newcastle terminal gate prices for %s", len(records), today)
        else:
            log.warning("TGP: no Newcastle prices found in table")
    except Exception as e:
        log.error("TGP fetch error: %s", e)


def get_tgp_history(days=30):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT date, fuel_type, price FROM terminal_gate_prices
        WHERE location='Newcastle' AND date >= DATE('now',?)
        ORDER BY date DESC""", (f"-{days} days",))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_latest_tgp():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT date, fuel_type, price FROM terminal_gate_prices
        WHERE location='Newcastle' AND date=(SELECT MAX(date) FROM terminal_gate_prices)
        ORDER BY fuel_type""")
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


def fetch_prices_nearby(fuel_type, token, lat=None, lng=None, radius=None):
    """Fetch prices from FuelCheck API for given coordinates and radius.
    Defaults to Newcastle home base if lat/lng not supplied."""
    lat    = lat    if lat    is not None else NEWCASTLE_LAT
    lng    = lng    if lng    is not None else NEWCASTLE_LNG
    radius = radius if radius is not None else SEARCH_RADIUS
    try:
        r = requests.post(
            NEARBY_URL,
            json={
                "fueltype":  fuel_type,
                "latitude":  lat,
                "longitude": lng,
                "radius":    radius,
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
        log.info("Fetched %d stations for %s within %dkm of (%.4f,%.4f)",
                 len(results), fuel_type, radius, lat, lng)
        return results
    except requests.RequestException as e:
        resp = e.response.text if hasattr(e, "response") and e.response is not None else "no response"
        log.error("FuelCheck nearby API error for %s: %s | %s", fuel_type, e, resp)
        return []


def fetch_and_store():
    """Scheduled job — always fetches Newcastle, stores to DB, fires ntfy alerts."""
    log.info("Starting scheduled Newcastle fetch at %s", datetime.now().isoformat())
    token = get_token()
    if not token:
        log.error("Aborting fetch — no valid token")
        return

    fuel_types = get_active_fuel_types()
    fetched_at = datetime.now().isoformat(timespec="seconds")
    records    = []
    cheapest   = {}

    for fuel_type in fuel_types:
        stations = fetch_prices_nearby(fuel_type, token)  # uses Newcastle defaults
        for s in stations:
            try:
                price  = float(s.get("price", 0))
                name   = s.get("name", "Unknown")
                addr   = s.get("address", "")
                suburb = s.get("suburb", "").title().strip()
                records.append((fetched_at, suburb, name, addr, fuel_type, price))
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

    conn = sqlite3.connect(DB_PATH)
    for fuel, info in cheapest.items():
        new_price = info["price"]
        label = ALL_FUEL_TYPES.get(fuel, fuel)
        msg = f"{label} {new_price:.1f}c — {info['station']}, {info['suburb']}"

        # Check last alerted price for this fuel type
        c = conn.cursor()
        c.execute("SELECT price FROM last_alerted_prices WHERE fuel_type=?", (fuel,))
        row = c.fetchone()
        last_price = row[0] if row else None

        # Only alert if price has changed AND is below threshold
        price_changed = (last_price is None or new_price != last_price)
        if price_changed:
            if new_price <= t290 and NTFY_290:
                send_ntfy_alert(NTFY_290, msg)
            if new_price <= t300 and NTFY_300:
                send_ntfy_alert(NTFY_300, msg)
            if new_price <= t315 and NTFY_315:
                send_ntfy_alert(NTFY_315, msg)
            # Update last alerted price regardless of threshold
            conn.execute("""INSERT INTO last_alerted_prices (fuel_type, price, alerted_at)
                VALUES (?,?,?) ON CONFLICT(fuel_type) DO UPDATE SET
                price=excluded.price, alerted_at=excluded.alerted_at""",
                (fuel, new_price, datetime.now().isoformat(timespec="seconds")))
            conn.commit()
            log.info("Price change detected for %s: %s -> %s", fuel, last_price, new_price)
        else:
            log.info("No price change for %s: still %.1fc — no alert sent", fuel, new_price)
    conn.close()


def send_ntfy_alert(url, message):
    try:
        requests.post(url, data=message.encode("utf-8"),
            headers={"Title": "Fuel Price Alert", "Priority": "default", "Tags": "fuelpump"}, timeout=5)
        log.info("ntfy alert sent to %s: %s", url, message)
    except requests.RequestException as e:
        log.error("ntfy error: %s", e)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", is_local=is_local_request())


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
    return jsonify({"all": ALL_FUEL_TYPES, "active": get_active_fuel_types()})


@app.route("/api/locality/search")
def api_locality_search():
    """Autocomplete endpoint — returns matching NSW suburbs/postcodes."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    results = search_locality(q)
    return jsonify(results)


@app.route("/api/search", methods=["POST"])
def api_search():
    """Live search for any NSW location — NOT stored to database."""
    body = request.json or {}
    try:
        lat    = float(body["lat"])
        lng    = float(body["lng"])
        radius = int(body.get("radius", 15))
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "lat, lng and radius are required"}), 400

    if radius not in VALID_RADII:
        return jsonify({"error": f"radius must be one of {VALID_RADII}"}), 400

    token = get_token()
    if not token:
        return jsonify({"error": "Could not obtain API token"}), 503

    # Fetch all fuel types (same set as configured for Newcastle)
    fuel_types = get_active_fuel_types()
    results = []
    for fuel_type in fuel_types:
        stations = fetch_prices_nearby(fuel_type, token, lat=lat, lng=lng, radius=radius)
        for s in stations:
            try:
                results.append({
                    "station":   s["name"],
                    "suburb":    s["suburb"],
                    "address":   s["address"],
                    "fuel_type": fuel_type,
                    "price":     float(s["price"]),
                })
            except (ValueError, TypeError):
                continue

    results.sort(key=lambda x: x["price"])
    return jsonify({
        "lat":     lat,
        "lng":     lng,
        "radius":  radius,
        "count":   len(results),
        "results": results,
    })


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
            row["fetched_at"], row["date"], row["suburb"], row["station"],
            row["address"], row["fuel_type"], row["price"],
            row["brent_usd"] or "", row["brent_aud"] or "",
        ])
    output.seek(0)
    filename = f"fuel_prices_{date.today().isoformat()}.csv"
    return Response(output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    fetch_and_store()
    fetch_and_store_crude()
    return jsonify({"status": "ok"})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})
@app.route("/api/tgp/latest")
def api_tgp_latest():
    return jsonify(get_latest_tgp())

@app.route("/api/tgp/history")
def api_tgp_history():
    return jsonify(get_tgp_history(30))


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(fetch_and_store, "cron", hour="7,17", minute=0)
    scheduler.add_job(fetch_and_store_crude, "cron", hour="7", minute=5)
    scheduler.add_job(fetch_and_store_tgp, "cron", hour="7", minute=10)
    scheduler.start()
    log.info("Scheduler started — fuel 07:00 & 17:00, crude 07:05 daily")


if __name__ == "__main__":
    init_db()
    load_localities()
    start_scheduler()
    fetch_and_store()
    fetch_and_store_crude()
    fetch_and_store_tgp()
    app.run(host="0.0.0.0", port=5000, debug=False)
