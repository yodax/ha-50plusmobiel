"""Constants for the 50+ Mobiel integration."""
from datetime import timedelta

DOMAIN = "mobiel50plus"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"

DEFAULT_SCAN_INTERVAL = timedelta(hours=1)

# Phase shift applied once, per install, to the first scheduled poll — see
# `poll_jitter()` in coordinator.py. Deliberately much smaller than
# DEFAULT_SCAN_INTERVAL: it spreads installs across the interval, it is not a
# second scheduling policy. 15 minutes is 900 whole-second buckets, orders of
# magnitude more spread than the plausible number of installs, so widening it
# would buy nothing measurable.
MAX_POLL_JITTER = timedelta(minutes=15)

# Last-resort config entry title, for the (unlikely) account whose API record
# has no first name and whose username has no local part to fall back on. The
# title must never be the username: Home Assistant slugifies it into the device
# name and from there into every entity_id and friendly_name, which would put
# the account's email address into any dashboard YAML a user shares.
DEFAULT_ACCOUNT_NAME = "50+ Mobiel account"
