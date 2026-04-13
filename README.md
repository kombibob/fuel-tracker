# NSW Fuel Tracker

Monitors fuel prices across NSW suburbs using the NSW FuelCheck API.
Runs as a Docker container on any Docker-capable host. Sends push
notifications to your phone via ntfy when prices drop below your threshold.

Includes a live web dashboard with price history charts and Brent crude
oil tracking.

## Requirements

- Docker and Docker Compose
- A free NSW FuelCheck API key from https://api.nsw.gov.au
- The ntfy app on your phone (optional, for price alerts)

## Setup

### 1. Get your FuelCheck API key

Register at https://api.nsw.gov.au — select the **Fuel API** product.
You will receive an API key and an API secret — you need both.

### 2. Clone the repo

```bash
git clone https://github.com/kombibob/fuel-tracker.git
cd fuel-tracker
```

### 3. Edit docker-compose.yml

Replace the following placeholders:

- `YOUR_API_KEY_HERE` → your FuelCheck API key
- `YOUR_API_SECRET_HERE` → your FuelCheck API secret
- `NTFY_290`, `NTFY_300`, `NTFY_315` → your ntfy topic URLs (or leave blank to disable)
- `ALERT_THRESHOLD` → your price alert threshold in cents/L

### 4. Create the data folder

Create a folder for the persistent SQLite database:

```bash
mkdir -p /your/data/path/fuel-tracker
```

Update the volume mount in `docker-compose.yml` to point to this folder:

```yaml
volumes:
  - /your/data/path/fuel-tracker:/data
```

### 5. Build and run

```bash
docker compose up -d --build
```

### 6. Access the dashboard

Open `http://your-host-ip:5010` in your browser.

## Configuration

All configuration is done via environment variables in `docker-compose.yml`:

| Variable | Description | Default |
|----------|-------------|---------|
| `FUELCHECK_API_KEY` | NSW FuelCheck API key | required |
| `FUELCHECK_API_SECRET` | NSW FuelCheck API secret | required |
| `NTFY_290` | ntfy topic URL for sub-290¢ alerts | blank |
| `NTFY_300` | ntfy topic URL for sub-300¢ alerts | blank |
| `NTFY_315` | ntfy topic URL for sub-315¢ alerts | blank |
| `ALERT_THRESHOLD` | Legacy single threshold (cents/L) | 305.0 |

## Customising fuel types

By default the tracker monitors `DL` (Diesel) and `PDL` (Premium Diesel).
To monitor different fuel types, edit `FUEL_TYPES` in `app.py`:

```python
FUEL_TYPES = ["U91", "E10"]   # example for unleaded petrol users
```

## Fuel type codes

| Code | Fuel type |
|------|-----------|
| `U91` | Unleaded 91 |
| `U95` | Premium Unleaded 95 |
| `U98` | Premium Unleaded 98 |
| `E10` | Ethanol 10% |
| `DL` | Diesel |
| `PDL` | Premium Diesel |
| `LPG` | LPG |

## Customising the search area

The tracker searches within 15km of Newcastle city centre by default.
To change the location and radius, edit these values in `app.py`:

```python
NEWCASTLE_LAT  = -32.9283
NEWCASTLE_LNG  = 151.7817
SEARCH_RADIUS  = 15   # kilometres
```

## Endpoints

| URL | Description |
|-----|-------------|
| `GET /` | Dashboard webpage |
| `GET /api/prices/latest` | Latest prices as JSON |
| `GET /api/prices/history` | 30-day price history as JSON |
| `GET /api/crude/latest` | Latest Brent crude price as JSON |
| `GET /api/crude/history` | 30-day Brent crude history as JSON |
| `GET /api/settings` | Get notification thresholds |
| `POST /api/settings` | Update notification thresholds |
| `POST /api/fetch` | Manually trigger a price fetch |
| `GET /health` | Health check |

## Schedule

Fuel prices are fetched at **7:00am and 5:00pm** daily (server time).
Brent crude is fetched at **7:05am** daily via Yahoo Finance.
Use the **Fetch now** button on the dashboard for an immediate update.

## Notifications

Uses ntfy for push notifications. Three alert tiers are supported:

- Subscribe to `fuel-290` for ultra-cheap alerts
- Subscribe to `fuel-300` for good price alerts
- Subscribe to `fuel-315` for general price awareness

Install the ntfy app on your phone and subscribe to topics on either
your self-hosted ntfy server or ntfy.sh (no account required).
Thresholds are adjustable from the dashboard without rebuilding.

## Data storage

Prices and Brent crude data are stored in a SQLite database at `/data/fuel.db`.
Map this to a persistent folder on your host via the volume mount in
`docker-compose.yml` to ensure data survives container restarts and rebuilds.
