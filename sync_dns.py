import docker
import requests
import time
import logging
import os
import sys
import signal
from threading import Event
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

# --- CONFIGURATION ---
class Config:
    TECHNITIUM_URL = os.getenv('TECHNITIUM_URL', '').rstrip('/')
    TECHNITIUM_TOKEN = os.getenv('TECHNITIUM_TOKEN', '')
    SYNC_INTERVAL = int(os.getenv('SYNC_INTERVAL', '60'))
    CACHE_REFRESH_INTERVAL = int(os.getenv('CACHE_REFRESH_INTERVAL', '3600')) 
    NETWORK_MAPPING_RAW = os.getenv('NETWORK_MAPPING', '')
    DRY_RUN = os.getenv('DRY_RUN', 'false').lower() in ('true', '1', 'yes', 'on')
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
    RECORD_EXPIRY_TTL = os.getenv('RECORD_EXPIRY_TTL', '').strip()
    RECORD_EXPIRY_REFRESH_BUFFER = os.getenv('RECORD_EXPIRY_REFRESH_BUFFER', '').strip()
    HEALTH_FILE = "/tmp/healthy"

# --- LOGGING SETUP ---
logging.basicConfig(
    level=Config.LOG_LEVEL, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("dockernet2dns")

# --- GLOBAL STATE ---
record_cache: Dict[str, str] = {}  # FQDN -> IP address
record_expiry: Dict[str, datetime] = {}  # FQDN -> expiry datetime from Technitium
record_zones: Dict[str, str] = {}  # FQDN -> zone name
last_cache_refresh = datetime.min.replace(tzinfo=timezone.utc)
exit_event = Event()

def handle_signal(signum, frame):
    """Sets the exit event to stop the main loop immediately."""
    logger.info("🛑 Shutdown signal received. Exiting gracefully...")
    exit_event.set()

def touch_health_file():
    """Updates the timestamp on the health file."""
    try:
        with open(Config.HEALTH_FILE, 'w') as f:
            f.write(str(time.time()))
    except Exception as e:
        logger.error(f"Failed to update health file: {e}")

def parse_network_mapping(raw_mapping: str) -> Dict[str, str]:
    """Parses 'net1:zone1,net2:zone2' string into a dictionary."""
    mapping = {}
    if not raw_mapping:
        return mapping
    
    parts = raw_mapping.split(',')
    for part in parts:
        if ':' in part:
            net, zone = part.split(':', 1)
            mapping[net.strip()] = zone.strip()
    return mapping

def parse_technitium_datetime(dt_str: str) -> Optional[datetime]:
    """Parse Technitium's datetime string (ISO 8601 format like '2025-03-07T12:34:56Z')."""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None

def fetch_zone_records(zone: str) -> None:
    """Downloads all A records for a zone to populate the local cache with expiry info."""
    url = f"{Config.TECHNITIUM_URL}/api/zones/records/get"
    params = {
        'token': Config.TECHNITIUM_TOKEN,
        'domain': zone,
        'zone': zone,
        'listZone': 'true'
    }
    
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        
        if data.get('status') != 'ok':
            logger.error(f"Failed to fetch zone '{zone}': {data.get('errorMessage')}")
            return

        count = 0
        if 'response' in data and 'records' in data['response']:
            for record in data['response']['records']:
                if record['type'] == 'A':
                    raw_name = record['name']
                    # Technitium API is inconsistent. It might return 'host' or 'host.domain.com'
                    # We normalize everything to FQDN for the cache key.
                    if raw_name == '@':
                        fqdn = zone
                    elif raw_name.endswith(f".{zone}"):
                        fqdn = raw_name
                    else:
                        fqdn = f"{raw_name}.{zone}"
                    
                    record_cache[fqdn] = record['rData']['ipAddress']
                    record_zones[fqdn] = zone
                    
                    # Extract expiry if present
                    expiry_str = record.get('expiryOn')
                    if expiry_str:
                        expiry_dt = parse_technitium_datetime(expiry_str)
                        if expiry_dt:
                            record_expiry[fqdn] = expiry_dt
                        else:
                            logger.warning(f"Could not parse expiry for {fqdn}: {expiry_str}")
                            record_expiry.pop(fqdn, None)
                    else:
                        record_expiry.pop(fqdn, None)
                    
                    count += 1
        
        logger.info(f"✔ RECONCILED: Loaded {count} existing records for '{zone}'")

    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching zone '{zone}': {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching zone '{zone}': {e}")

def update_dns_record(fqdn: str, ip_address: str, zone: str, reason: str = "drift") -> bool:
    """Sends a request to Technitium to add/update an A record.
    
    Args:
        fqdn: Fully qualified domain name
        ip_address: IP address to set
        zone: Zone name
        reason: Reason for update ("drift" or "expiry_refresh")
    """
    if Config.DRY_RUN:
        logger.info(f"[DRY RUN] Would update DNS ({reason}): {fqdn} -> {ip_address}")
        return True

    url = f"{Config.TECHNITIUM_URL}/api/zones/records/add"
    params = {
        'token': Config.TECHNITIUM_TOKEN,
        'domain': fqdn,
        'zone': zone,
        'type': 'A',
        'ipAddress': ip_address,
        'overwrite': 'true',
        'ttl': 300
    }

    # Keep expiry disabled by default; add it only when explicitly configured.
    if Config.RECORD_EXPIRY_TTL:
        params['expiryTtl'] = Config.RECORD_EXPIRY_TTL
    
    try:
        r = requests.post(url, data=params, timeout=10)
        r.raise_for_status()
        response = r.json()
        
        if response.get('status') == 'ok':
            logger.info(f"✔ UPDATED ({reason}): {fqdn} -> {ip_address}")
            
            # Update expiry info if configured
            if Config.RECORD_EXPIRY_TTL:
                try:
                    expiry_ttl = int(Config.RECORD_EXPIRY_TTL)
                    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=expiry_ttl)
                    record_expiry[fqdn] = new_expiry
                except (ValueError, TypeError):
                    pass
            
            return True
        else:
            logger.error(f"✖ API ERROR {fqdn}: {response.get('errorMessage')}")
            return False
            
    except requests.exceptions.RequestException as e:
        logger.error(f"✖ CONNECTION ERROR {fqdn}: {e}")
        return False

def main():
    global last_cache_refresh
    
    # 1. Register Signal Handlers
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # 2. Validate Config
    if Config.DRY_RUN:
        logger.warning("!!! DRY RUN MODE ENABLED - NO CHANGES WILL BE MADE !!!")

    if not Config.TECHNITIUM_URL or not Config.TECHNITIUM_TOKEN:
        logger.critical("Missing required env vars: TECHNITIUM_URL or TECHNITIUM_TOKEN")
        sys.exit(1)

    # Validate and parse RECORD_EXPIRY_TTL
    expiry_ttl = None
    if Config.RECORD_EXPIRY_TTL:
        try:
            expiry_ttl = int(Config.RECORD_EXPIRY_TTL)
            if expiry_ttl <= 0:
                raise ValueError("must be greater than zero")
        except ValueError:
            logger.critical("RECORD_EXPIRY_TTL must be a positive integer when set")
            sys.exit(1)
    
    # Validate and parse RECORD_EXPIRY_REFRESH_BUFFER
    refresh_buffer = Config.SYNC_INTERVAL  # Default to SYNC_INTERVAL
    if Config.RECORD_EXPIRY_REFRESH_BUFFER:
        try:
            refresh_buffer = int(Config.RECORD_EXPIRY_REFRESH_BUFFER)
            if refresh_buffer < 0:
                raise ValueError("must be >= 0")
        except ValueError:
            logger.critical("RECORD_EXPIRY_REFRESH_BUFFER must be a non-negative integer when set")
            sys.exit(1)
    
    # Check for edge cases with expiry and buffer
    if expiry_ttl:
        if refresh_buffer < 10:
            logger.warning(
                f"⚠️  RECORD_EXPIRY_REFRESH_BUFFER ({refresh_buffer}s) is very small. "
                f"Ensure this provides enough time for API calls to complete before record expiry."
            )
        if refresh_buffer > expiry_ttl:
            logger.warning(
                f"⚠️  RECORD_EXPIRY_REFRESH_BUFFER ({refresh_buffer}s) exceeds RECORD_EXPIRY_TTL ({expiry_ttl}s). "
                f"Records will be refreshed immediately after creation. Consider reducing the buffer."
            )

    net_map = parse_network_mapping(Config.NETWORK_MAPPING_RAW)
    if not net_map:
        logger.critical("No NETWORK_MAPPING provided.")
        sys.exit(1)

    # 3. Connect to Docker
    try:
        client = docker.from_env()
        client.ping()
    except Exception as e:
        logger.critical(f"Could not connect to Docker socket: {e}")
        sys.exit(1)

    logger.info(f"--- dockernet2dns Started ---")
    logger.info(f"Networks: {list(net_map.keys())}")
    logger.info(f"Interval: {Config.SYNC_INTERVAL}s")
    logger.info(
        "Record expiry: %s",
        f"enabled ({Config.RECORD_EXPIRY_TTL}s, refresh buffer: {refresh_buffer}s)" if expiry_ttl else "disabled"
    )

    # 4. Main Loop with Dynamic Wait
    last_sync_time = datetime.min.replace(tzinfo=timezone.utc)
    
    while not exit_event.is_set():
        try:
            now = datetime.now(timezone.utc)
            
            # --- Calculate next wake times ---
            next_sync_time = last_sync_time + timedelta(seconds=Config.SYNC_INTERVAL)
            
            # Find the soonest expiry refresh deadline
            next_expiry_deadline = datetime.max.replace(tzinfo=timezone.utc)
            if expiry_ttl:
                for fqdn, expiry_dt in record_expiry.items():
                    refresh_deadline = expiry_dt - timedelta(seconds=refresh_buffer)
                    if refresh_deadline < next_expiry_deadline:
                        next_expiry_deadline = refresh_deadline
            
            # Wait until the soonest event (sync or expiry refresh)
            next_event = min(next_sync_time, next_expiry_deadline)
            wait_seconds = max(0, (next_event - now).total_seconds())
            
            # Dynamic wait - returns immediately if exit_event is set
            if exit_event.wait(wait_seconds):
                break  # Shutdown signal received
            
            # Re-check current time after wait
            now = datetime.now(timezone.utc)
            
            # --- Phase A: Cache Refresh (if sync interval elapsed) ---
            if now - last_cache_refresh > timedelta(seconds=Config.CACHE_REFRESH_INTERVAL):
                if last_cache_refresh != datetime.min.replace(tzinfo=timezone.utc):
                    logger.info("Refreshing DNS Cache from Server...")
                record_cache.clear()
                record_expiry.clear()
                record_zones.clear()
                # Use set(values) to avoid fetching same zone twice
                for zone in set(net_map.values()):
                    fetch_zone_records(zone)
                last_cache_refresh = datetime.now(timezone.utc)

            # --- Phase B: Docker Sync (if sync interval elapsed) ---
            if now >= next_sync_time:
                containers = client.containers.list()
                
                for container in containers:
                    hostname = container.labels.get('dns.hostname', container.name)
                    net_config = container.attrs.get('NetworkSettings', {}).get('Networks', {})

                    for net_name, zone in net_map.items():
                        if net_name in net_config:
                            ip = net_config[net_name].get('IPAddress')
                            if not ip: continue

                            fqdn = hostname if zone in hostname else f"{hostname}.{zone}"
                            cached_ip = record_cache.get(fqdn)
                            
                            if cached_ip != ip:
                                if not Config.DRY_RUN:
                                    logger.info(f"Drift: {fqdn} (Cache: {cached_ip or 'None'}) -> (Docker: {ip})")
                                
                                if update_dns_record(fqdn, ip, zone, reason="drift"):
                                    record_cache[fqdn] = ip
                                    record_zones[fqdn] = zone
                            else:
                                logger.debug(f"Skipping {fqdn}: match.")
                
                last_sync_time = now
            
            # --- Phase C: Expiry Refresh (if any records need refresh) ---
            if expiry_ttl:
                for fqdn, expiry_dt in list(record_expiry.items()):
                    refresh_deadline = expiry_dt - timedelta(seconds=refresh_buffer)
                    if now >= refresh_deadline:
                        ip = record_cache.get(fqdn)
                        zone = record_zones.get(fqdn)
                        
                        if ip and zone:
                            logger.info(f"⏱️  EXPIRY REFRESH: {fqdn} expires at {expiry_dt.isoformat()}")
                            update_dns_record(fqdn, ip, zone, reason="expiry_refresh")
                        else:
                            logger.warning(f"Cannot refresh {fqdn}: missing IP or zone info")
            
            # --- Phase D: Report Health ---
            touch_health_file()

        except requests.exceptions.RequestException as e:
            logger.error(f"Global Network Error (Technitium unreachable?): {e}")
        except docker.errors.APIError as e:
            logger.error(f"Docker API Error: {e}")
        except Exception as e:
            logger.error(f"Unexpected Loop Error: {e}", exc_info=True)
    
    logger.info("👋 Sync Service Stopped.")

if __name__ == "__main__":
    main()
