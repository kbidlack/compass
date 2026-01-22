"""
Compass Client - Python client for the rendezvous service.

This module provides a high-level Python interface for connecting to
a Compass rendezvous server and discovering Minecraft peers.

After discovering a peer's node_id, you handle the direct P2P connection
yourself (e.g., using pyroh directly).

Example:
    >>> import asyncio
    >>> from compass import CompassClient, ConnectionRequest
    >>>
    >>> async def on_connection_request(request: ConnectionRequest):
    ...     print(f"Connection request from: {request.username}")
    ...     # Accept the connection - both parties get each other's node_id
    ...     await request.accept()
    >>>
    >>> async def main():
    ...     async with CompassClient(server_node_id="...") as client:
    ...         await client.register(
    ...             mc_uuid="your-uuid",
    ...             mc_username="YourName",
    ...             mc_access_token="your-access-token"
    ...         )
    ...         await client.set_discoverable()
    ...
    ...         # Listen for incoming connection requests
    ...         await client.listen(on_connection_request)
    >>>
    >>> asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import httpx
import iroh
import pyroh

__version__ = "0.1.0"
__all__ = [
    "CompassClient",
    "PeerInfo",
    "ConnectionRequest",
    "create_client",
    "CompassError",
    "ConnectionError",
    "AuthenticationError",
    "ProtocolError",
    "PeerUnavailableError",
    "RENDEZVOUS_ALPN",
    "ConnectionRequestHandler",
]

# ALPN protocol identifier for the rendezvous service
RENDEZVOUS_ALPN = b"rendezvous/1"

# Mojang session server URL
MOJANG_SESSION_URL = "https://sessionserver.mojang.com/session/minecraft/join"

# Default timeout for connection requests (seconds)
DEFAULT_REQUEST_TIMEOUT = 60.0

# Type alias for connection request handlers
ConnectionRequestHandler = Callable[["ConnectionRequest"], Awaitable[None]]


@dataclass
class PeerInfo:
    """Information about a discovered peer (after connection is accepted)."""

    uuid: str
    username: str
    node_id: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PeerInfo:
        """Create a PeerInfo from a dictionary."""
        return cls(
            uuid=data["uuid"],
            username=data["username"],
            node_id=data["node_id"],
        )


class ConnectionRequest:
    """
    Represents an incoming connection request from another peer.

    Contains only the peer's uuid and username - their node_id is NOT
    shared until you accept the connection.

    Use accept() to accept the connection, or decline() to reject it.
    """

    def __init__(
        self,
        uuid: str,
        username: str,
        reason: str | None,
        client: "CompassClient",
    ) -> None:
        self.uuid = uuid
        self.username = username
        self.reason = reason
        self._client = client
        self._handled = False

    async def accept(self) -> PeerInfo:
        """
        Accept this connection request.

        After accepting, both you and the requester receive each other's
        full peer info (including node_id).

        Returns:
            PeerInfo for the requesting peer.

        Raises:
            ProtocolError: If the accept fails.
        """
        if self._handled:
            raise ProtocolError("Connection request already handled")
        self._handled = True
        return await self._client._accept_connection(self.uuid)

    async def decline(self) -> None:
        """
        Decline this connection request.

        The requester will receive a generic "request failed" error
        (they cannot tell if you declined vs. timeout vs. offline).
        """
        if self._handled:
            raise ProtocolError("Connection request already handled")
        self._handled = True
        await self._client._decline_connection(self.uuid)


class CompassError(Exception):
    """Base exception for Compass client errors."""

    pass


class ConnectionError(CompassError):
    """Error connecting to the rendezvous server."""

    pass


class AuthenticationError(CompassError):
    """Error during Minecraft authentication."""

    pass


class ProtocolError(CompassError):
    """Protocol-level error from the server."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Server error: {reason}")


class PeerUnavailableError(CompassError):
    """
    The target peer is unavailable.

    This is intentionally vague for privacy - could mean:
    - Peer is offline
    - Peer is not discoverable
    - Peer declined the connection
    - Request timed out
    """

    pass


class CompassClient:
    """
    Async client for the Compass rendezvous service.

    This client connects to a Compass rendezvous server and provides
    methods for registering a Minecraft identity, becoming discoverable,
    and discovering peers.

    Example usage:

        async def on_request(request: ConnectionRequest):
            print(f"Request from: {request.username}")
            if should_accept(request):
                peer = await request.accept()
                print(f"Connected! Node ID: {peer.node_id}")
            else:
                await request.decline()

        async with CompassClient(server_node_id="...") as client:
            await client.register(
                mc_uuid="...",
                mc_username="...",
                mc_access_token="..."
            )
            await client.set_discoverable()

            # Option 1: Request a specific peer (waits for them to accept)
            try:
                peer = await client.request_peer(target_uuid="...", timeout=30.0)
            except PeerUnavailableError:
                print("Peer unavailable or declined")

            # Option 2: Listen for incoming requests
            await client.listen(on_request)
    """

    def __init__(
        self,
        server_node_id: str,
        server_relay_url: Optional[str] = None,
        node: Optional[pyroh.Iroh] = None,
    ) -> None:
        """
        Initialize the Compass client.

        Args:
            server_node_id: The iroh NodeId of the rendezvous server.
            server_relay_url: Optional relay URL for the server.
            node: Optional pre-configured Iroh node to use. If not provided,
                a new node will be created with default discovery settings.
                Pass your own node if you need to configure additional protocols
                or custom settings.
        """
        self._server_node_id_str = server_node_id
        self._server_relay_url = server_relay_url
        self._external_node = node

        self._conn_nonce: Optional[str] = None
        self._registered = False
        self._discoverable = False
        self._iroh_node: Optional[pyroh.Iroh] = None
        self._owns_node = False  # Track if we created the node (and should clean it up)
        self._reader: Optional[pyroh.StreamReader] = None
        self._writer: Optional[pyroh.StreamWriter] = None
        self._request_handler: Optional[ConnectionRequestHandler] = None
        self._listen_task: Optional[asyncio.Task[None]] = None
        self._pending_responses: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # Track pending peer requests: target_uuid -> Future[PeerInfo]
        self._pending_peer_requests: dict[str, asyncio.Future[PeerInfo]] = {}

    @property
    def server_node_id(self) -> str:
        """Get the server's endpoint key (NodeId)."""
        return self._server_node_id_str

    @property
    def node(self) -> Optional[pyroh.Iroh]:
        """Get the Iroh node used by this client (available after connect)."""
        return self._iroh_node

    @property
    def is_connected(self) -> bool:
        """Check if connected to the server."""
        return self._conn_nonce is not None

    @property
    def is_registered(self) -> bool:
        """Check if registered with the server."""
        return self._registered

    @property
    def is_discoverable(self) -> bool:
        """Check if currently discoverable."""
        return self._discoverable

    async def _send_message(self, msg: dict[str, Any]) -> None:
        """Send a JSON message to the server."""
        if self._writer is None:
            raise ConnectionError("Not connected to server")

        line = json.dumps(msg) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()

    async def _recv_message_raw(self) -> dict[str, Any]:
        """Receive a single JSON message from the server."""
        if self._reader is None:
            raise ConnectionError("Not connected to server")

        line_bytes = await self._reader.readline()
        if not line_bytes:
            raise ConnectionError("Connection closed by server")

        line = line_bytes.decode("utf-8").strip()
        if not line:
            raise ConnectionError("Empty message from server")

        try:
            msg: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as e:
            raise ProtocolError(f"Invalid JSON: {e}")

        return msg

    async def _recv_message(self) -> dict[str, Any]:
        """
        Receive a message, handling notifications if a handler is set.

        If listening is active, this pulls from the pending queue.
        Otherwise, reads directly from the stream.
        """
        if self._listen_task is not None:
            # We're in listen mode, get from queue
            msg = await self._pending_responses.get()
        else:
            # Direct read
            msg = await self._recv_message_raw()

        if msg.get("type") == "error":
            raise ProtocolError(msg.get("reason", "unknown"))

        return msg

    async def _listen_loop(self) -> None:
        """Background loop that receives messages and dispatches notifications."""
        try:
            while True:
                msg = await self._recv_message_raw()
                msg_type = msg.get("type")

                if msg_type == "connection_request":
                    # Incoming connection request - call the handler as a task
                    # so we don't block the listen loop (handler may call accept/decline)
                    if self._request_handler is not None:
                        request = ConnectionRequest(
                            uuid=msg["uuid"],
                            username=msg["username"],
                            reason=msg.get("reason"),
                            client=self,
                        )
                        # Schedule handler as a task - don't await it
                        asyncio.create_task(self._safe_call_handler(request))

                elif msg_type == "peer":
                    # Peer info received (after accept)
                    peer = PeerInfo.from_dict(msg)
                    peer_uuid = peer.uuid

                    # Resolve any pending request for this peer
                    if peer_uuid in self._pending_peer_requests:
                        future = self._pending_peer_requests.pop(peer_uuid)
                        if not future.done():
                            future.set_result(peer)
                    else:
                        # Unsolicited peer info (e.g., from accepting a request)
                        # Put in queue for other handlers
                        await self._pending_responses.put(msg)

                elif msg_type == "request_failed":
                    # A request we made was declined/timed out
                    # Fail all pending peer request futures
                    for future in self._pending_peer_requests.values():
                        if not future.done():
                            future.set_exception(PeerUnavailableError())
                    self._pending_peer_requests.clear()

                elif msg_type == "error":
                    # Error message - put in queue for handlers
                    await self._pending_responses.put(msg)

                else:
                    # Other message - put in queue for request handlers
                    await self._pending_responses.put(msg)

        except ConnectionError:
            pass  # Connection closed
        except asyncio.CancelledError:
            pass
        finally:
            # Clean up state so other methods know we're not listening anymore
            self._listen_task = None
            self._request_handler = None
            # Fail any pending peer requests
            for future in self._pending_peer_requests.values():
                if not future.done():
                    future.set_exception(ConnectionError("Listen loop terminated"))
            self._pending_peer_requests.clear()

    async def _safe_call_handler(self, request: ConnectionRequest) -> None:
        """Safely call the request handler, catching any exceptions."""
        try:
            if self._request_handler is not None:
                await self._request_handler(request)
        except Exception:
            pass  # Don't crash on handler errors

    async def connect(self) -> None:
        """
        Connect to the rendezvous server.

        Raises:
            ConnectionError: If connection fails.
        """
        if self._conn_nonce is not None:
            return  # Already connected

        try:
            # Create node address
            addr = pyroh.node_addr(
                self._server_node_id_str,
                relay_url=self._server_relay_url,
            )

            # Use external node if provided, otherwise create our own
            if self._external_node is not None:
                self._iroh_node = self._external_node
                self._owns_node = False
            else:
                # Create and store the iroh node to keep it alive
                # Use DEFAULT discovery so we can find nodes by NodeId without needing relay URL
                options = pyroh.NodeOptions(
                    node_discovery=iroh.NodeDiscoveryConfig.DEFAULT
                )
                self._iroh_node = await pyroh.Iroh.memory_with_options(options)
                self._owns_node = True

            # Connect using our persistent node
            self._reader, self._writer = await pyroh.connect(
                addr,
                alpn=RENDEZVOUS_ALPN,
                node=self._iroh_node,
            )

            # Send an initial message to trigger the lazy stream creation
            # In QUIC/iroh, the server's accept_bi() only returns after client sends data
            self._writer.write(b'{"type":"init"}\n')
            await self._writer.drain()

            # Receive the HELLO message with connection nonce
            hello = await self._recv_message_raw()
            if hello.get("type") != "hello":
                raise ProtocolError(f"Expected hello, got {hello.get('type')}")

            self._conn_nonce = hello.get("conn_nonce")
            if not self._conn_nonce:
                raise ProtocolError("No connection nonce in hello")

        except Exception as e:
            self._conn_nonce = None
            self._reader = None
            self._writer = None
            if self._owns_node:
                self._iroh_node = None
            if isinstance(e, (ConnectionError, ProtocolError)):
                raise
            raise ConnectionError(f"Failed to connect: {e}") from e

    async def _mojang_session_join(
        self,
        access_token: str,
        uuid: str,
        server_id: str,
    ) -> None:
        """
        Call Mojang's session/join endpoint to prove ownership of an account.

        This is the client side of Minecraft's authentication protocol.
        """
        # Normalize UUID (remove dashes)
        uuid_clean = uuid.replace("-", "")

        payload = {
            "accessToken": access_token,
            "selectedProfile": uuid_clean,
            "serverId": server_id,
        }

        async with httpx.AsyncClient() as http:
            response = await http.post(
                MOJANG_SESSION_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )

            # 204 No Content = success
            # 403 = invalid token or already used server_id
            if response.status_code == 204:
                return

            # Try to get error details
            try:
                error_data = response.json()
                error_msg = error_data.get("errorMessage", "Unknown error")
            except Exception:
                error_msg = f"HTTP {response.status_code}"

            raise AuthenticationError(f"Mojang session join failed: {error_msg}")

    async def register(
        self,
        mc_uuid: str,
        mc_username: str,
        mc_access_token: str,
    ) -> None:
        """
        Register a Minecraft identity with the rendezvous server.

        This involves:
        1. Sending your UUID/username to the server
        2. Receiving a challenge (server_id)
        3. Calling Mojang's API to prove you own the account
        4. Confirming verification with the server

        Args:
            mc_uuid: Your Minecraft UUID.
            mc_username: Your Minecraft username.
            mc_access_token: Your Minecraft access token (from launcher).

        Raises:
            ConnectionError: If not connected.
            AuthenticationError: If Mojang verification fails.
            ProtocolError: If server rejects registration.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to server")

        # Step 1: Send register request
        await self._send_message(
            {
                "type": "register",
                "mc_uuid": mc_uuid,
                "mc_username": mc_username,
                "conn_nonce": self._conn_nonce,
            }
        )

        # Step 2: Receive challenge
        response = await self._recv_message()
        if response.get("type") != "challenge":
            raise ProtocolError(f"Expected challenge, got {response.get('type')}")

        server_id = response.get("server_id")
        if not server_id:
            raise ProtocolError("No server_id in challenge")

        # Step 3: Call Mojang to prove ownership
        await self._mojang_session_join(mc_access_token, mc_uuid, server_id)

        # Step 4: Confirm verification
        await self._send_message(
            {
                "type": "verify",
                "conn_nonce": self._conn_nonce,
            }
        )

        response = await self._recv_message()
        if response.get("type") != "registered":
            raise ProtocolError(f"Expected registered, got {response.get('type')}")

        self._registered = True

    async def set_discoverable(self) -> None:
        """
        Make yourself discoverable by other peers.

        Other players can then send you connection requests.

        Raises:
            ConnectionError: If not connected.
            ProtocolError: If request fails.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to server")

        await self._send_message(
            {
                "type": "discoverable",
                "conn_nonce": self._conn_nonce,
            }
        )

        response = await self._recv_message()
        if response.get("type") != "discoverable_ack":
            raise ProtocolError(
                f"Expected discoverable_ack, got {response.get('type')}"
            )

        self._discoverable = True

    async def set_undiscoverable(self) -> None:
        """
        Make yourself undiscoverable.

        Other players will no longer be able to send you connection requests.

        Raises:
            ConnectionError: If not connected.
            ProtocolError: If request fails.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to server")

        await self._send_message(
            {
                "type": "undiscoverable",
                "conn_nonce": self._conn_nonce,
            }
        )

        response = await self._recv_message()
        if response.get("type") != "undiscoverable_ack":
            raise ProtocolError(
                f"Expected undiscoverable_ack, got {response.get('type')}"
            )

        self._discoverable = False

    async def request_peer(
        self,
        target_uuid: str,
        request_reason: str | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> PeerInfo:
        """
        Request connection to another peer.

        This sends a connection request to the target peer. The request will
        wait until the peer accepts (or declines/times out).

        Args:
            target_uuid: The Minecraft UUID of the peer to connect to.
            request_reason: Optional reason for the connection request. This will be
                shown to the peer when they receive your request.
            timeout: How long to wait for the peer to accept (seconds).

        Returns:
            PeerInfo containing the peer's node_id for direct connection.

        Raises:
            ConnectionError: If not connected.
            PeerUnavailableError: If peer is unavailable, declines, or times out.
            ProtocolError: If protocol error occurs.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to server")

        # Send the connect request
        message: dict[str, Any] = {
            "type": "connect",
            "to_uuid": target_uuid,
            "conn_nonce": self._conn_nonce,
        }
        if request_reason is not None:
            message["reason"] = request_reason
        await self._send_message(message)

        # If we're in listen mode, set up a future and wait for the response
        if self._listen_task is not None and not self._listen_task.done():
            loop = asyncio.get_event_loop()
            future: asyncio.Future[PeerInfo] = loop.create_future()
            self._pending_peer_requests[target_uuid] = future

            try:
                # First, wait for request_sent acknowledgment
                ack = await asyncio.wait_for(self._pending_responses.get(), timeout=5.0)
                if ack.get("type") == "error":
                    reason = ack.get("reason", "unknown")
                    if reason == "peer_unavailable":
                        raise PeerUnavailableError()
                    raise ProtocolError(reason)
                if ack.get("type") != "request_sent":
                    raise ProtocolError(f"Expected request_sent, got {ack.get('type')}")

                # Now wait for the peer response via the future
                # The listen loop will resolve this future when a peer message arrives
                return await asyncio.wait_for(future, timeout=timeout)

            except asyncio.TimeoutError:
                raise PeerUnavailableError()
            finally:
                self._pending_peer_requests.pop(target_uuid, None)

        else:
            # Not in listen mode - read responses directly
            try:
                # Wait for request_sent acknowledgment
                response = await asyncio.wait_for(self._recv_message_raw(), timeout=5.0)
                if response.get("type") == "error":
                    reason = response.get("reason", "unknown")
                    if reason == "peer_unavailable":
                        raise PeerUnavailableError()
                    raise ProtocolError(reason)
                if response.get("type") != "request_sent":
                    raise ProtocolError(
                        f"Expected request_sent, got {response.get('type')}"
                    )

                # Wait for peer response
                response = await asyncio.wait_for(
                    self._recv_message_raw(), timeout=timeout
                )
                if response.get("type") == "peer":
                    return PeerInfo.from_dict(response)
                elif response.get("type") == "request_failed":
                    raise PeerUnavailableError()
                elif response.get("type") == "error":
                    reason = response.get("reason", "unknown")
                    if reason == "peer_unavailable":
                        raise PeerUnavailableError()
                    raise ProtocolError(reason)
                else:
                    raise ProtocolError(f"Unexpected response: {response.get('type')}")

            except asyncio.TimeoutError:
                raise PeerUnavailableError()

    async def _accept_connection(self, from_uuid: str) -> PeerInfo:
        """
        Accept a pending connection request (internal method).

        Called by ConnectionRequest.accept().
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to server")

        await self._send_message(
            {
                "type": "accept",
                "from_uuid": from_uuid,
                "conn_nonce": self._conn_nonce,
            }
        )

        # Wait for accept_ack
        if self._listen_task is not None and not self._listen_task.done():
            response = await self._pending_responses.get()
        else:
            response = await self._recv_message_raw()

        if response.get("type") == "error":
            raise ProtocolError(response.get("reason", "unknown"))
        if response.get("type") != "accept_ack":
            raise ProtocolError(f"Expected accept_ack, got {response.get('type')}")

        # Now wait for the peer info (sent via notification channel)
        if self._listen_task is not None and not self._listen_task.done():
            response = await self._pending_responses.get()
        else:
            response = await self._recv_message_raw()

        if response.get("type") == "peer":
            return PeerInfo.from_dict(response)
        elif response.get("type") == "error":
            raise ProtocolError(response.get("reason", "unknown"))
        else:
            raise ProtocolError(f"Expected peer, got {response.get('type')}")

    async def _decline_connection(self, from_uuid: str) -> None:
        """
        Decline a pending connection request (internal method).

        Called by ConnectionRequest.decline().
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to server")

        await self._send_message(
            {
                "type": "decline",
                "from_uuid": from_uuid,
                "conn_nonce": self._conn_nonce,
            }
        )

        # Wait for decline_ack
        if self._listen_task is not None and not self._listen_task.done():
            response = await self._pending_responses.get()
        else:
            response = await self._recv_message_raw()

        if response.get("type") == "error":
            raise ProtocolError(response.get("reason", "unknown"))
        if response.get("type") != "decline_ack":
            raise ProtocolError(f"Expected decline_ack, got {response.get('type')}")

    async def listen(self, handler: ConnectionRequestHandler) -> None:
        """
        Listen for incoming connection requests.

        This runs until the connection is closed or disconnect() is called.
        When another peer requests to connect to you, your handler is called
        with a ConnectionRequest object. You can then accept() or decline().

        Args:
            handler: Async callback called with ConnectionRequest when a peer
                     wants to connect.
                     Signature: async def handler(request: ConnectionRequest) -> None

        Example:
            async def on_request(request: ConnectionRequest):
                print(f"Request from: {request.username}")
                if request.reason:
                    print(f"Reason: {request.reason}")
                # Accept the connection
                peer = await request.accept()
                print(f"Connected! Node ID: {peer.node_id}")

            await client.listen(on_request)
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to server")

        self._request_handler = handler
        self._listen_task = asyncio.create_task(self._listen_loop())

        try:
            await self._listen_task
        except asyncio.CancelledError:
            pass
        finally:
            self._listen_task = None
            self._request_handler = None

    def start_listening(self, handler: ConnectionRequestHandler) -> None:
        """
        Start listening for connection requests in the background.

        Unlike listen(), this returns immediately and runs in the background.
        Use stop_listening() to stop.

        Args:
            handler: Async callback called with ConnectionRequest when a peer
                     wants to connect.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to server")

        # Clear any stale data from previous listen sessions
        while not self._pending_responses.empty():
            try:
                self._pending_responses.get_nowait()
            except asyncio.QueueEmpty:
                break

        self._request_handler = handler
        self._listen_task = asyncio.create_task(self._listen_loop())

    def stop_listening(self) -> None:
        """Stop listening for connection requests."""
        if self._listen_task is not None:
            self._listen_task.cancel()
            self._listen_task = None
        self._request_handler = None

    async def ping(self) -> None:
        """
        Send a heartbeat to keep the session alive.

        Raises:
            ConnectionError: If not connected.
            ProtocolError: If ping fails.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to server")

        await self._send_message(
            {
                "type": "ping",
                "conn_nonce": self._conn_nonce,
            }
        )

        response = await self._recv_message()
        if response.get("type") != "pong":
            raise ProtocolError(f"Expected pong, got {response.get('type')}")

    async def disconnect(self) -> None:
        """Disconnect from the server and clean up resources."""
        self.stop_listening()

        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None

        self._reader = None
        # Only release the iroh node if we created it
        if self._owns_node:
            self._iroh_node = None
        self._conn_nonce = None
        self._registered = False
        self._discoverable = False

    async def __aenter__(self) -> "CompassClient":
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.disconnect()


async def create_client(
    server_node_id: str,
    server_relay_url: Optional[str] = None,
    node: Optional[pyroh.Iroh] = None,
) -> CompassClient:
    """
    Create and connect a CompassClient.

    This is a convenience function that creates a client and connects it.

    Args:
        server_node_id: The iroh NodeId of the rendezvous server.
        server_relay_url: Optional relay URL for the server.
        node: Optional pre-configured Iroh node to use.

    Returns:
        A connected CompassClient.
    """
    client = CompassClient(
        server_node_id=server_node_id,
        server_relay_url=server_relay_url,
        node=node,
    )
    await client.connect()
    return client
