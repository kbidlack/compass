# Compass Client

Python client for the Compass rendezvous service - privacy-preserving peer discovery for Minecraft players.

## Installation

```bash
uv add .
```

## Requirements

- Python 3.14+
- [pyroh](https://github.com/kbidlack/pyroh)
- [httpx](https://pypi.org/project/httpx/)

## Usage

### Basic: Request a Specific Peer

```python
import asyncio
from compass import CompassClient

async def main():
    async with CompassClient(server_node_id="<server-node-id>") as client:
        await client.register(
            mc_uuid="your-minecraft-uuid",
            mc_username="YourMinecraftName",
            mc_access_token="your-minecraft-access-token",
        )
        await client.set_discoverable()

        # Request a specific peer (blocks until mutual consent)
        peer = await client.request_peer(target_uuid="friend-minecraft-uuid")
        print(f"Peer node_id: {peer.node_id}")
        # Now use pyroh/iroh to connect directly

asyncio.run(main())
```

### Listen for Incoming Peer Requests

```python
import asyncio
from compass import CompassClient, PeerInfo

async def on_peer(peer: PeerInfo):
    """Called when another player requests to connect to you."""
    print(f"Peer connected: {peer.username}")
    print(f"Node ID: {peer.node_id}")
    # Use pyroh/iroh to connect directly to peer.node_id

async def main():
    async with CompassClient(server_node_id="<server-node-id>") as client:
        await client.register(
            mc_uuid="your-minecraft-uuid",
            mc_username="YourMinecraftName",
            mc_access_token="your-minecraft-access-token",
        )
        await client.set_discoverable()

        # Listen for incoming peer requests (runs until disconnect)
        await client.listen(on_peer)

asyncio.run(main())
```

### Background Listening

```python
async def main():
    async with CompassClient(server_node_id="...") as client:
        await client.register(...)
        await client.set_discoverable()

        # Start listening in background
        client.start_listening(on_peer)

        # You can still make requests while listening
        peer = await client.request_peer(target_uuid="...")

        # Stop when done
        client.stop_listening()
```

## API

### `CompassClient`

```python
client = CompassClient(
    server_node_id: str,           # Rendezvous server's node ID
    server_relay_url: str | None,  # Optional relay URL
)
```

| Method | Description |
|--------|-------------|
| `await connect()` | Connect to the rendezvous server |
| `await disconnect()` | Disconnect and clean up |
| `await register(mc_uuid, mc_username, mc_access_token)` | Register and verify Minecraft identity |
| `await set_discoverable()` | Make yourself discoverable |
| `await set_undiscoverable()` | Make yourself not discoverable |
| `await request_peer(target_uuid, reason=None)` | Request peer connection, returns `PeerInfo` |
| `await listen(handler)` | Listen for peer requests (blocks) |
| `start_listening(handler)` | Start listening in background |
| `stop_listening()` | Stop background listening |
| `await ping()` | Keep session alive |

### `PeerInfo`

```python
@dataclass
class PeerInfo:
    uuid: str            # Minecraft UUID
    username: str        # Minecraft username
    node_id: str         # iroh NodeId for direct connection
    reason: str | None   # Reason provided by peer for connection (optional)
```

### `PeerHandler`

```python
PeerHandler = Callable[[PeerInfo], Awaitable[None]]
```

### Exceptions

| Exception | Description |
|-----------|-------------|
| `CompassError` | Base exception |
| `ConnectionError` | Connection failed |
| `AuthenticationError` | Mojang auth failed |
| `ProtocolError` | Protocol error from server |

## How It Works

1. `register()` sends your identity, receives a challenge, calls Mojang's session API, then verifies
2. `set_discoverable()` makes you visible to other players
3. When both players request each other, the server notifies both with peer info
4. Use the returned `node_id` with pyroh/iroh for direct P2P connection

## License

MIT