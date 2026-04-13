# Newcastle Fuel Tracker

Monitors diesel and premium diesel prices across Newcastle suburbs using the
NSW FuelCheck API. Runs as a Docker container on Unraid. Sends phone alerts
via ntfy when prices drop below your threshold.

## Setup

### 1. Get your FuelCheck API key
Register at https://api.nsw.gov.au — select the Fuel API product.

### 2. Edit docker-compose.yml
Replace the following placeholders:
- `YOUR_API_KEY_HERE` → your FuelCheck API key
- `http://your-unraid-ip:port/fuel-alerts` → your ntfy server URL (or leave blank)
- `305.0` → your price alert threshold in cents/L

### 3. Create the appdata folder on Unraid
```
mkdir -p /mnt/user/appdata/fuel-tracker
```

### 4. Build and run
Copy the fuel-tracker folder to your Unraid server, then:
```
cd fuel-tracker
docker compose up -d --build
```

### 5. Access the dashboard
Open http://your-unraid-ip:5010 in your browser.

## Endpoints
| URL | Description |
|-----|-------------|
| `GET /` | Dashboard webpage |
| `GET /api/prices/latest` | Latest prices as JSON |
| `GET /api/prices/history` | 30-day history as JSON |
| `POST /api/fetch` | Manually trigger a price fetch |
| `GET /health` | Health check |

## Schedule
Prices are fetched automatically at 7:00am and 5:00pm daily (server time).
Use the "Fetch now" button on the dashboard for an immediate update.

## Notifications
Uses ntfy for push notifications to your phone.
- Install the ntfy app on your phone
- Point NTFY_URL to your self-hosted ntfy container, or use ntfy.sh (cloud)
- An alert fires when any price drops below ALERT_THRESHOLD

## Data storage
SQLite database at `/data/fuel.db` (mapped to Unraid appdata).
Backs up automatically with the rest of your appdata.

## Fuel type codes
- `DL`  = Standard Diesel
- `PDL` = Premium Diesel
