# Compass - Rendezvous Service

A privacy-preserving peer discovery service for Minecraft players using iroh P2P networking.

Disclaimer: Most of this code was written by AI and so I will not claim to guarantee anything about it. I mostly just need it for another personal project


## Overview

Compass enables Minecraft players to discover and connect to each other without exposing their IP addresses until mutual consent is established. The rendezvous server runs as an iroh node, providing:

- **Zero trust in server** — Server cannot impersonate clients or read P2P traffic
- **Zero persistent state** — All data expires; nothing written to disk
- **Zero IP exposure** — IPs never stored; peers only learn IPs after mutual consent
- **Minimal protocol** — iroh handles auth; server just verifies Minecraft ownership

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         iroh Network                            │
│                                                                 │
│   ┌──────────┐         ┌─────────────┐         ┌──────────┐    │
│   │ Client A │◄───────►│ Rendezvous  │◄───────►│ Client B │    │
│   │ (Python) │  iroh   │   (Rust)    │  iroh   │ (Python) │    │
│   └──────────┘         └──────┬──────┘         └──────────┘    │
│                               │                                 │
└───────────────────────────────┼─────────────────────────────────┘
                                │ HTTPS (verification only)
                                ▼
                        ┌──────────────┐
                        │ Mojang API   │
                        └──────────────┘
```

## Project Structure

```
compass/
├── README.md              # This file
├── backend/               # Rust rendezvous server
│   ├── Cargo.toml
│   ├── Cargo.lock
│   └── src/
│       ├── lib.rs         # Library exports
│       ├── main.rs        # Server binary
│       ├── handler.rs     # Protocol handler
│       ├── protocol.rs    # Message types
│       ├── state.rs       # Server state management
│       └── mojang.rs      # Mojang API client
└── client-python/         # Python client
    ├── pyproject.toml     # Package config
    ├── README.md          # Client documentation
    ├── compass.py         # Client library
    └── test_rendezvous.py # End-to-end tests
```

## Version Requirements

> **Important:** The Rust backend and Python client must use compatible iroh versions.

| Component | iroh Version |
|-----------|--------------|
| Backend (Rust) | `0.35.x` |
| Client (Python) | `0.35.0` |

Both the server and client use **n0 DNS discovery** (`iroh.link`) to find each other by NodeId. This means clients only need the server's NodeId to connect — no relay URL required.

## Building the Server

### Prerequisites

- Rust 1.70+ (install via [rustup](https://rustup.rs/))

### Build

```bash
cd backend
cargo build --release
```

The binary will be at `backend/target/release/compass`.

## Running the Server

```bash
cd backend

# Run with ephemeral key (for testing)
cargo run --release

# Run with persistent key (recommended for production)
cargo run --release -- --key-file server.key

# Custom cleanup interval (default: 60 seconds)
cargo run --release -- --cleanup-interval 120
```

### Command-Line Options

| Option | Description |
|--------|-------------|
| `-k, --key-file <PATH>` | Path to secret key file (auto-generated if missing) |
| `--cleanup-interval <SECS>` | Interval for cleaning expired sessions (default: 60) |

### Output

On startup, the server prints its NodeId and Relay URL:

```
Server NodeId: fc35e88216d28e4b5668cc8ec1e01fb34f0b17a58ffb1377261e6a0992bc0bd4
Relay URL: https://use1-1.relay.iroh.network./
Rendezvous server is running
```

Clients use the **NodeId** to connect. The server publishes its address to n0 DNS discovery, so clients can find it automatically.

## Python Client

The Python client provides a high-level async interface for connecting to the rendezvous server.

### Installation

```bash
cd client-python

# Using uv (recommended)
uv sync

# Or using pip
pip install -e .
```

### Requirements

- Python 3.14+
- [iroh](https://pypi.org/project/iroh/) 0.35.0 - iroh FFI bindings
- [pyroh](https://github.com/kbidlack/pyroh) - asyncio wrapper for iroh
- [httpx](https://pypi.org/project/httpx/) - HTTP client for Mojang API

### Quick Start

```python
import asyncio
from compass import CompassClient

async def main():
    # Connect using just the server's NodeId
    # (discovery finds the server automatically)
    client = CompassClient(
        server_node_id="fc35e88216d28e4b5668cc8ec1e01fb34f0b17a58ffb1377261e6a0992bc0bd4"
    )
    
    await client.connect()
    
    # Register with Minecraft identity
    await client.register(
        mc_uuid="your-minecraft-uuid",
        mc_username="YourName",
        mc_access_token="your-access-token"
    )
    
    # Become discoverable
    await client.set_discoverable()
    
    # Request connection to another player (requires mutual consent)
    peer = await client.request_peer(target_uuid="friend-minecraft-uuid")
    print(f"Connected to {peer.username} at {peer.node_id}")
    
    await client.disconnect()

asyncio.run(main())
```

See [client-python/README.md](client-python/README.md) for full API documentation.

## Protocol

All messages are JSON over iroh ALPN protocol `rendezvous/1`.

### Message Flow

1. **Connection**: Client connects → Server sends `hello` with connection nonce
2. **Registration**: Client sends `register` → Server sends `challenge`
3. **Verification**: Client completes Mojang auth, sends `verify` → Server sends `registered`
4. **Discovery**: Client sends `discoverable` → Server sends `discoverable_ack`
5. **Connection**: Client sends `connect` → Server sends `peer` (after mutual consent) or `waiting`

### Messages

```json
// Client → Server
{ "type": "register", "mc_uuid": "...", "mc_username": "...", "conn_nonce": "..." }
{ "type": "verify", "conn_nonce": "..." }
{ "type": "discoverable", "conn_nonce": "..." }
{ "type": "undiscoverable", "conn_nonce": "..." }
{ "type": "connect", "to_uuid": "...", "conn_nonce": "...", "reason": "..." }
{ "type": "ping", "conn_nonce": "..." }

// Server → Client
{ "type": "hello", "conn_nonce": "..." }
{ "type": "challenge", "server_id": "..." }
{ "type": "registered" }
{ "type": "discoverable_ack" }
{ "type": "undiscoverable_ack" }
{ "type": "peer", "uuid": "...", "username": "...", "node_id": "...", "reason": "..." }
{ "type": "waiting" }
{ "type": "pong" }
{ "type": "error", "reason": "..." }
```

## How Discovery Works

Compass uses iroh's **n0 DNS discovery** service:

1. The server enables `.discovery_n0()` on its endpoint
2. This publishes the server's relay URL and direct addresses to `iroh.link` DNS
3. Clients also enable `NodeDiscoveryConfig.DEFAULT` 
4. When connecting, clients query DNS to find the server by NodeId
5. No hardcoded relay URLs needed — just the NodeId

This means:
- Server operators only need to share their NodeId
- Clients automatically find the best path to the server
- Works across NATs via relay servers

## License

MIT
