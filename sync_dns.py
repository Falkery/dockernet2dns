import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Dict, List, Optional, Set, Tuple

import docker
import requests

# --- LOGGING SETUP ---
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO').upper(), 
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("dockernet2dns")

exit_event = Event()

def handle_signal(signum, frame):
    """Sets the exit event to stop the main loop immediately."""
    logger.info("🛑 Shutdown signal received. Exiting gracefully...")
    exit_event.set()

class Config:
    def __init__(self):
        self.technitium_url = os.getenv('TECHNITIUM_URL', '').rstrip('/')
        self.technitium_token = os.getenv('TECHNITIUM_TOKEN', '')
        self.sync_interval = int(os.getenv('SYNC_INTERVAL', '60'))
        self.cache_refresh_interval = int(os.getenv('CACHE_REFRESH_INTERVAL', '3600')) 
        self.dry_run = os.getenv('DRY_RUN', 'false').lower() in ('true', '1', 'yes', 'on')
        self.health_file = "/tmp/healthy"
        
        # Expiry Settings
        self.record_expiry_ttl = self._parse_int_env('RECORD_EXPIRY_TTL')
        self.record_expiry_refresh_buffer = self._parse_int_env('RECORD_EXPIRY_REFRESH_BUFFER', default=self.sync_interval)
        
        # Dead Container Settings
        self.dead_container_strategy = os.getenv('DEAD_CONTAINER_STRATEGY', 'ignore').lower()
        self.shortened_expiry_ttl = int(os.getenv('SHORTENED_EXPIRY_TTL', '60'))
        
        # Networking
        self.net_map, self.managed_zones = self._parse_network_mapping(os.getenv('NETWORK_MAPPING', ''))

    def _parse_int_env(self, key: str, default: Optional[int] = None) -> Optional[int]:
        val = os.getenv(key, '').strip()
        if not val:
            return default
        try:
            return int(val)
        except ValueError:
            logger.critical(f"{key} must be an integer when set.")
            sys.exit(1)

    def _parse_network_mapping(self, raw_mapping: str) -> Tuple[Dict[str, str], Set[str]]:
        mapping = {}
        managed_zones = set()
        if not raw_mapping:
            return mapping, managed_zones
        
        for part in raw_mapping.split(','):
            if ':' in part:
                segments = part.split(':')
                net = segments[0].strip()
                zone = segments[1].strip()
                mapping[net] = zone
                if len(segments) > 2 and segments[2].strip().lower() == 'managed':
                    managed_zones.add(zone)
        return mapping, managed_zones

    def validate(self):
        if self.dry_run:
            logger.warning("!!! DRY RUN MODE ENABLED - NO CHANGES WILL BE MADE !!!")

        if not self.technitium_url or not self.technitium_token:
            logger.critical("Missing required env vars: TECHNITIUM_URL or TECHNITIUM_TOKEN")
            sys.exit(1)

        if not self.net_map:
            logger.critical("No NETWORK_MAPPING provided.")
            sys.exit(1)

        if self.record_expiry_ttl is not None and self.record_expiry_ttl <= 0:
            logger.critical("RECORD_EXPIRY_TTL must be a positive integer when set")
            sys.exit(1)

        if self.record_expiry_refresh_buffer is not None and self.record_expiry_refresh_buffer < 0:
            logger.critical("RECORD_EXPIRY_REFRESH_BUFFER must be a non-negative integer when set")
            sys.exit(1)

        if self.dead_container_strategy not in ('ignore', 'shorten', 'remove'):
            logger.critical(f"Invalid DEAD_CONTAINER_STRATEGY: {self.dead_container_strategy}")
            sys.exit(1)

        # Check for edge cases with expiry and buffer
        if self.record_expiry_ttl:
            buffer = self.record_expiry_refresh_buffer
            if buffer < 10:
                logger.warning(
                    f"⚠️  RECORD_EXPIRY_REFRESH_BUFFER ({buffer}s) is very small. "
                    f"Ensure this provides enough time for API calls to complete before record expiry."
                )
            if buffer > self.record_expiry_ttl:
                logger.warning(
                    f"⚠️  RECORD_EXPIRY_REFRESH_BUFFER ({buffer}s) exceeds RECORD_EXPIRY_TTL ({self.record_expiry_ttl}s). "
                    f"Records will be refreshed immediately after creation. Consider reducing the buffer."
                )

class DnsState:
    """Encapsulates all local state and caches."""
    def __init__(self):
        self.cache: Dict[str, str] = {}         # FQDN -> IP address
        self.expiry: Dict[str, datetime] = {}   # FQDN -> expiry datetime
        self.zones: Dict[str, str] = {}         # FQDN -> zone name
        self.managed: Dict[str, bool] = {}      # FQDN -> True if managed by dockernet2dns
        
        self.active_fqdns: Set[str] = set()
        self.last_cache_refresh = datetime.min.replace(tzinfo=timezone.utc)
        self.last_sync_time = datetime.min.replace(tzinfo=timezone.utc)
        
    def clear_records(self):
        self.cache.clear()
        self.expiry.clear()
        self.zones.clear()
        self.managed.clear()
        
    def backup(self) -> Dict:
        return {
            'cache': self.cache.copy(),
            'expiry': self.expiry.copy(),
            'zones': self.zones.copy(),
            'managed': self.managed.copy()
        }
        
    def restore(self, backup: Dict):
        self.cache.update(backup['cache'])
        self.expiry.update(backup['expiry'])
        self.zones.update(backup['zones'])
        self.managed.update(backup['managed'])

class TechnitiumClient:
    """Handles all interactions with the Technitium DNS API."""
    def __init__(self, config: Config, state: DnsState):
        self.cfg = config
        self.state = state

    @staticmethod
    def _parse_datetime(dt_str: str) -> Optional[datetime]:
        if not dt_str: return None
        try:
            return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return None

    def fetch_zone_records(self, zone: str) -> bool:
        url = f"{self.cfg.technitium_url}/api/zones/records/get"
        params = {
            'token': self.cfg.technitium_token,
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
                return False

            count = 0
            if 'response' in data and 'records' in data['response']:
                for record in data['response']['records']:
                    if record['type'] == 'A':
                        raw_name = record['name']
                        fqdn = zone if raw_name == '@' else (raw_name if raw_name.endswith(f".{zone}") else f"{raw_name}.{zone}")
                        
                        self.state.cache[fqdn] = record['rData']['ipAddress']
                        self.state.zones[fqdn] = zone
                        self.state.managed[fqdn] = (record.get('comments') == 'dockernet2dns')
                        
                        expiry_str = record.get('expiryOn')
                        if expiry_str:
                            expiry_dt = self._parse_datetime(expiry_str)
                            if expiry_dt:
                                self.state.expiry[fqdn] = expiry_dt
                            else:
                                logger.warning(f"Could not parse expiry for {fqdn}: {expiry_str}")
                                self.state.expiry.pop(fqdn, None)
                        else:
                            self.state.expiry.pop(fqdn, None)
                        
                        count += 1
            
            logger.info(f"✔ RECONCILED: Loaded {count} existing records for '{zone}'")
            return True

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error fetching zone '{zone}': {e}")
            return False

    def update_record(self, fqdn: str, ip_address: str, zone: str, reason: str = "drift", override_ttl: int = None) -> bool:
        if self.cfg.dry_run:
            logger.info(f"[DRY RUN] Would update DNS ({reason}): {fqdn} -> {ip_address}")
            return True

        url = f"{self.cfg.technitium_url}/api/zones/records/add"
        params = {
            'token': self.cfg.technitium_token,
            'domain': fqdn,
            'zone': zone,
            'type': 'A',
            'ipAddress': ip_address,
            'overwrite': 'true',
            'ttl': 300,
            'comments': 'dockernet2dns'
        }

        expiry_to_use = override_ttl if override_ttl is not None else self.cfg.record_expiry_ttl
        if expiry_to_use is not None:
            params['expiryTtl'] = str(expiry_to_use)
        
        try:
            r = requests.post(url, data=params, timeout=10)
            r.raise_for_status()
            response = r.json()
            
            if response.get('status') == 'ok':
                logger.info(f"✔ UPDATED ({reason}): {fqdn} -> {ip_address}")
                
                if expiry_to_use is not None:
                    try:
                        self.state.expiry[fqdn] = datetime.now(timezone.utc) + timedelta(seconds=expiry_to_use)
                    except (ValueError, TypeError):
                        pass
                
                self.state.managed[fqdn] = True
                return True
            else:
                logger.error(f"✖ API ERROR {fqdn}: {response.get('errorMessage')}")
                return False
                
        except requests.exceptions.RequestException as e:
            logger.error(f"✖ CONNECTION ERROR {fqdn}: {e}")
            return False

    def delete_record(self, fqdn: str, zone: str) -> bool:
        if self.cfg.dry_run:
            logger.info(f"[DRY RUN] Would delete DNS record: {fqdn}")
            return True

        url = f"{self.cfg.technitium_url}/api/zones/records/delete"
        params = {
            'token': self.cfg.technitium_token,
            'domain': fqdn,
            'zone': zone,
            'type': 'A'
        }
        
        try:
            r = requests.post(url, data=params, timeout=10)
            r.raise_for_status()
            response = r.json()
            
            if response.get('status') == 'ok':
                logger.info(f"✔ DELETED (dead container): {fqdn}")
                return True
            else:
                logger.error(f"✖ API ERROR deleting {fqdn}: {response.get('errorMessage')}")
                return False
                
        except requests.exceptions.RequestException as e:
            logger.error(f"✖ CONNECTION ERROR deleting {fqdn}: {e}")
            return False

class DockerScanner:
    """Handles communication with the Docker Daemon."""
    def __init__(self, config: Config):
        self.cfg = config
        try:
            self.client = docker.from_env()
            self.client.ping()
        except Exception as e:
            logger.critical(f"Could not connect to Docker socket: {e}")
            sys.exit(1)

    def scan_active_containers(self) -> List[Tuple[str, str, str]]:
        """Returns a list of (fqdn, ip, zone) for running containers mapped to zones."""
        results = []
        containers = self.client.containers.list()
        
        for container in containers:
            hostname = container.labels.get('dns.hostname', container.name)
            net_config = container.attrs.get('NetworkSettings', {}).get('Networks', {})

            for net_name, zone in self.cfg.net_map.items():
                if net_name in net_config:
                    ip = net_config[net_name].get('IPAddress')
                    if not ip: continue
                    fqdn = hostname if hostname.endswith(f".{zone}") or hostname == zone else f"{hostname}.{zone}"
                    results.append((fqdn, ip, zone))
        return results

class SyncApp:
    """Orchestrates the main application logic."""
    def __init__(self):
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
        
        self.cfg = Config()
        self.cfg.validate()
        
        self.state = DnsState()
        self.api = TechnitiumClient(self.cfg, self.state)
        self.docker = DockerScanner(self.cfg)

    def touch_health_file(self) -> None:
        try:
            with open(self.cfg.health_file, 'w') as f:
                f.write(str(time.time()))
        except Exception as e:
            logger.error(f"Failed to update health file: {e}")

    def refresh_cache(self, now: datetime) -> None:
        if now - self.state.last_cache_refresh <= timedelta(seconds=self.cfg.cache_refresh_interval):
            return

        if self.state.last_cache_refresh != datetime.min.replace(tzinfo=timezone.utc):
            logger.info("Refreshing DNS Cache from Server...")
        
        backup = self.state.backup()
        self.state.clear_records()
        
        success = True
        for zone in set(self.cfg.net_map.values()):
            if not self.api.fetch_zone_records(zone):
                success = False
        
        if success:
            self.state.last_cache_refresh = datetime.now(timezone.utc)
        else:
            logger.warning("Cache refresh failed partially, restoring previous cache state")
            self.state.restore(backup)

    def sync_docker_containers(self) -> None:
        current_active_fqdns = set()
        containers = self.docker.scan_active_containers()
        
        now = datetime.now(timezone.utc)
        
        for fqdn, ip, zone in containers:
            current_active_fqdns.add(fqdn)
            cached_ip = self.state.cache.get(fqdn)
            
            needs_update = False
            reason = "drift"
            
            if cached_ip != ip:
                needs_update = True
            else:
                expiry_dt = self.state.expiry.get(fqdn)
                if expiry_dt and now >= expiry_dt:
                    needs_update = True
                    reason = "recreate_expired"
                elif not self.state.managed.get(fqdn, False):
                    needs_update = True
                    reason = "adopt_record"
            
            if needs_update:
                if not self.cfg.dry_run:
                    if reason == "drift":
                        logger.info(f"Drift: {fqdn} (Cache: {cached_ip or 'None'}) -> (Docker: {ip})")
                    elif reason == "adopt_record":
                        logger.info(f"Adopting unmanaged record: {fqdn} ({ip})")
                    else:
                        logger.info(f"Recreating expired record: {fqdn} ({ip})")
                
                if self.api.update_record(fqdn, ip, zone, reason=reason):
                    self.state.cache[fqdn] = ip
                    self.state.zones[fqdn] = zone
            else:
                logger.debug(f"Skipping {fqdn}: match.")
                
        self.state.active_fqdns.clear()
        self.state.active_fqdns.update(current_active_fqdns)

    def sweep_dead_containers(self, now: datetime) -> None:
        if self.cfg.dead_container_strategy == 'ignore' and not self.cfg.managed_zones:
            return

        for fqdn, ip in list(self.state.cache.items()):
            if fqdn not in self.state.active_fqdns:
                zone = self.state.zones.get(fqdn)
                if not zone: continue
                
                is_managed_zone = zone in self.cfg.managed_zones
                is_managed_record = self.state.managed.get(fqdn, False)
                
                action = 'ignore'
                if is_managed_zone:
                    action = 'remove'
                elif is_managed_record:
                    action = self.cfg.dead_container_strategy
                
                if action == 'remove':
                    if self.api.delete_record(fqdn, zone):
                        self.state.cache.pop(fqdn, None)
                        self.state.expiry.pop(fqdn, None)
                        self.state.zones.pop(fqdn, None)
                        self.state.managed.pop(fqdn, None)
                elif action == 'shorten':
                    current_expiry = self.state.expiry.get(fqdn)
                    needs_shortening = True
                    if current_expiry:
                        time_left = (current_expiry - now).total_seconds()
                        if time_left <= self.cfg.shortened_expiry_ttl + 5:
                            needs_shortening = False
                    
                    if needs_shortening:
                        self.api.update_record(fqdn, ip, zone, reason="shorten", override_ttl=self.cfg.shortened_expiry_ttl)

    def refresh_expiring_records(self, now: datetime) -> None:
        if not self.cfg.record_expiry_ttl:
            return

        for fqdn, expiry_dt in list(self.state.expiry.items()):
            if fqdn not in self.state.active_fqdns:
                continue
                
            refresh_deadline = expiry_dt - timedelta(seconds=self.cfg.record_expiry_refresh_buffer)
            if now >= refresh_deadline:
                ip = self.state.cache.get(fqdn)
                zone = self.state.zones.get(fqdn)
                
                if ip and zone:
                    logger.info(f"⏱️  EXPIRY REFRESH: {fqdn} expires at {expiry_dt.isoformat()}")
                    if not self.api.update_record(fqdn, ip, zone, reason="expiry_refresh"):
                        logger.warning(f"Refresh failed for {fqdn}, backing off for 30s")
                        self.state.expiry[fqdn] = now + timedelta(seconds=30) + timedelta(seconds=self.cfg.record_expiry_refresh_buffer)
                else:
                    logger.warning(f"Cannot refresh {fqdn}: missing IP or zone info")

    def run(self) -> None:
        logger.info(f"--- dockernet2dns Started ---")
        logger.info(f"Networks: {list(self.cfg.net_map.keys())}")
        logger.info(f"Interval: {self.cfg.sync_interval}s")
        logger.info(
            "Record expiry: %s",
            f"enabled ({self.cfg.record_expiry_ttl}s, refresh buffer: {self.cfg.record_expiry_refresh_buffer}s)" if self.cfg.record_expiry_ttl else "disabled"
        )
        
        while not exit_event.is_set():
            try:
                now = datetime.now(timezone.utc)
                
                # --- Calculate next wake times ---
                next_sync_time = self.state.last_sync_time + timedelta(seconds=self.cfg.sync_interval)
                next_expiry_deadline = datetime.max.replace(tzinfo=timezone.utc)
                
                if self.cfg.record_expiry_ttl:
                    for fqdn, expiry_dt in self.state.expiry.items():
                        if fqdn not in self.state.active_fqdns:
                            continue
                        refresh_deadline = expiry_dt - timedelta(seconds=self.cfg.record_expiry_refresh_buffer)
                        if refresh_deadline < next_expiry_deadline:
                            next_expiry_deadline = refresh_deadline
                
                next_event = min(next_sync_time, next_expiry_deadline)
                wait_seconds = max(0, (next_event - now).total_seconds())
                
                if exit_event.wait(wait_seconds):
                    break
                
                now = datetime.now(timezone.utc)
                
                self.refresh_cache(now)
                
                if now >= next_sync_time:
                    self.state.last_sync_time = now
                    self.sync_docker_containers()
                    self.sweep_dead_containers(now)
                
                self.refresh_expiring_records(now)
                self.touch_health_file()

            except requests.exceptions.RequestException as e:
                logger.error(f"Global Network Error (Technitium unreachable?): {e}")
            except docker.errors.APIError as e:
                logger.error(f"Docker API Error: {e}")
            except Exception as e:
                logger.error(f"Unexpected Loop Error: {e}", exc_info=True)
        
        logger.info("👋 Sync Service Stopped.")

if __name__ == "__main__":
    app = SyncApp()
    app.run()
