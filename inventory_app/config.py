import os
from datetime import timedelta

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Local dev: read SECRET_KEY / ADMIN_PASSWORD / etc. from sendy_erp/.env
# (sibling of inventory_app/). Railway sets these via the env var dashboard
# and ignores the missing file. override=False keeps real env vars authoritative.
load_dotenv(os.path.join(BASE_DIR, '..', '.env'), override=False)

# Railway provides a persistent volume — set DATA_DIR env var to that mount path.
# Falls back to local instance/ folder for development.
_data_dir = os.environ.get('DATA_DIR', os.path.join(BASE_DIR, 'instance'))
DATABASE_PATH = os.path.join(_data_dir, 'inventory.db')

LOW_STOCK_DEFAULT_THRESHOLD = 10
ITEMS_PER_PAGE = 50
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'imports')

# Override via Railway environment variables.
# No committed fallbacks — fail loudly so a stray local run can't
# silently boot with publicly-known secrets.
def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} environment variable is required. "
            f"For local dev, copy .env.example to .env and set values."
        )
    return value


SECRET_KEY     = _require_env('SECRET_KEY')
ADMIN_PASSWORD = _require_env('ADMIN_PASSWORD')

# Session config — "จำฉันไว้" persists for 30 days.
# When user ticks the checkbox at /login, the route sets session.permanent=True
# which makes the cookie outlive the browser tab and use this lifetime.
PERMANENT_SESSION_LIFETIME = timedelta(days=30)
SESSION_COOKIE_HTTPONLY    = True
SESSION_COOKIE_SAMESITE    = 'Lax'
# Secure cookie only when serving over HTTPS (Railway prod). Local dev (http)
# would silently drop the cookie if this were True. Toggle via env when deployed.
SESSION_COOKIE_SECURE      = os.environ.get('SESSION_COOKIE_SECURE', '').lower() in ('1', 'true', 'yes')
