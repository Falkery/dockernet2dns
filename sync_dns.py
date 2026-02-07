import docker
import requests
import time
import logging
import os
import sys
import signal
from threading import Event
from datetime import datetime, timedelta
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
    HEALTH_FILE = "/tmp/healthy"

# --- LOGGING SETUP ---
logging.basicConfig(
    level=Config.LOG_LEVEL, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("dockernet2dns")

# --- GLOBAL STATE ---
record_cache: Dict[str, str] = {}
last_cache_refresh = datetime.min
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

def fetch_zone_records(zone: str) -> None:
    """Downloads all A records for a zone to populate the local cache."""
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
                    count += 1
        
        logger.info(f"✔ RECONCILED: Loaded {count} existing records for '{zone}'")

    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching zone '{zone}': {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching zone '{zone}': {e}")

def update_dns_record(fqdn: str, ip_address: str, zone: str) -> bool:
    """Sends a request to Technitium to add/update an A record."""
    if Config.DRY_RUN:
        logger.info(f"[DRY RUN] Would update DNS: {fqdn} -> {ip_address}")
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
    
    try:
        r = requests.post(url, data=params, timeout=10)
        r.raise_for_status()
        response = r.json()
        
        if response.get('status') == 'ok':
            logger.info(f"✔ UPDATED: {fqdn} -> {ip_address}")
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

    # 4. Main Loop
    while not exit_event.is_set():
        try:
            # --- Phase A: Cache Refresh ---
            if datetime.now() - last_cache_refresh > timedelta(seconds=Config.CACHE_REFRESH_INTERVAL):
                if last_cache_refresh != datetime.min:
                    logger.info("Refreshing DNS Cache from Server...")
                record_cache.clear()
                # Use set(values) to avoid fetching same zone twice
                for zone in set(net_map.values()):
                    fetch_zone_records(zone)
                last_cache_refresh = datetime.now()

            # --- Phase B: Docker Sync ---
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
                            
                            if update_dns_record(fqdn, ip, zone):
                                record_cache[fqdn] = ip 
                        else:
                            logger.debug(f"Skipping {fqdn}: match.")
            
            # --- Phase C: Report Health ---
            touch_health_file()

        except requests.exceptions.RequestException as e:
             logger.error(f"Global Network Error (Technitium unreachable?): {e}")
        except docker.errors.APIError as e:
             logger.error(f"Docker API Error: {e}")
        except Exception as e:
            logger.error(f"Unexpected Loop Error: {e}", exc_info=True)
        
        exit_event.wait(Config.SYNC_INTERVAL)
    
    logger.info("👋 Sync Service Stopped.")

if __name__ == "__main__":
    main()
