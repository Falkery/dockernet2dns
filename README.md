# dockernet2dns

A lightweight, "no-nonsense" container that watches your Docker containers and automatically updates DNS records in [Technitium DNS Server](https://technitium.com/dns/).

![Build Status](https://img.shields.io/github/actions/workflow/status/Falkery/dockernet2dns/publish.yml?branch=main)
![License](https://img.shields.io/github/license/Falkery/dockernet2dns)
![Python Version](https://img.shields.io/badge/python-3.11+-blue.svg)

## Why use this?

Most Docker DNS updaters are designed for Traefik or simple bridge networks. If you use **Macvlan** or **IPvlan** to give your containers real LAN IP addresses, those tools often fail because they grab the internal Docker IP (172.x) instead of your public LAN IP (192.168.x).

**dockernet2dns** is designed specifically to solve this.

* 🚀 **IPvlan/Macvlan Support:** Target specific network interfaces to get the correct IP.
* 🧠 **Smart Caching:** Reconciles with Technitium on startup and only calls the API when an IP actually changes. Zero API spam.
* 🛡️ **Resilient:** Handles Technitium restarts and network blips gracefully.
* 🧪 **Dry Run:** Test your mapping without breaking your DNS.
* ❤️ **Lightweight:** Uses the official `python:3-slim` image (~50MB).

## Quick Start (Docker Compose)

```yaml
services:
  dockernet2dns:
    image: ghcr.io/falkery/dockernet2dns:latest
    container_name: dockernet2dns
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      # --- Technitium Config ---
      - TECHNITIUM_URL=http://192.168.1.10:5380
      - TECHNITIUM_TOKEN=your_api_token_here
      
      # --- Network Mapping ---
      # format: "DockerNetworkName:DNSZoneName"
      # multiple: "net1:zone1,net2:zone2"
      - NETWORK_MAPPING=ipvlan_iot:iot.lan,ipvlan_server:server.lan
      
      # --- Options ---
      - SYNC_INTERVAL=60
      - DRY_RUN=false
```

## Configuration

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TECHNITIUM_URL` | *Required* | Base URL of your DNS server (e.g. `http://10.0.0.1:5380`) |
| `TECHNITIUM_TOKEN` | *Required* | API Token with Write permissions |
| `NETWORK_MAPPING` | *Required* | Comma separated list of `docker_network:dns_zone` |
| `SYNC_INTERVAL` | `60` | How often to check for changes (in seconds) |
| `CACHE_REFRESH_INTERVAL` | `3600` | How often to re-download the full zone from server to fix manual drift (seconds) |
| `DRY_RUN` | `false` | If `true`, logs intended changes but does not call API |
| `LOG_LEVEL` | `INFO` | Set to `DEBUG` for verbose logging (shows skipped records) |

## How it works

1.  **Mounts Docker Socket:** It reads the `NetworkSettings` of your containers.
2.  **Matches Network:** It checks if a container is attached to one of the networks defined in `NETWORK_MAPPING`.
3.  **Determines Hostname:** It looks for a label `dns.hostname`. If not found, it falls back to the container name.
4.  **Syncs:** If the IP in Docker is different from the DNS record, it updates Technitium.

## Usage Tips

### 1. Setting a Custom Hostname
By default, the script uses the container name (e.g., `my-plex-server` becomes `my-plex-server.lan`). To use a different DNS name, add a label to your container:

```yaml
services:
  plex:
    image: plexinc/pms-docker
    labels:
      - "dns.hostname=media" # Creates media.lan
```

### 2. IPvlan/Macvlan Setup
Ensure the network name in `NETWORK_MAPPING` matches the name of the network *inside* the container, not necessarily the global Docker network name (though they are usually the same).

### 3. Healthcheck
The container includes a built-in HEALTHCHECK. It monitors the internal loop and will mark the container as `unhealthy` if the sync process hangs for more than 120 seconds.

## License

MIT License. See [LICENSE](LICENSE) for details.
