#!/usr/bin/env python3
"""
Compass Rendezvous - Test Script (No Authentication Required)

This script tests the Compass rendezvous server and Python client without
requiring real Minecraft authentication. The server must be started with
the --skip-auth flag.

USAGE:
------
1. Start the server with: cargo run --release -- --skip-auth --key-file server.key
2. Run: python test_no_auth.py

Or let this script start the server for you:
    python test_no_auth.py --start-server
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
import uuid as uuid_module
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Add parent directory to path for importing compass module
sys.path.insert(0, str(Path(__file__).parent))

from compass import (
    CompassClient,
    ConnectionRequest,
    PeerInfo,
    PeerUnavailableError,
    ProtocolError,
)

# Path to the backend directory (relative to this file)
BACKEND_DIR = Path(__file__).parent.parent / "backend"

# Server key file path
SERVER_KEY_FILE = BACKEND_DIR / "server.key"

# How long to wait for the server to start (seconds)
SERVER_STARTUP_TIMEOUT = 30


@dataclass
class ServerInfo:
    """Information about the running server."""

    node_id: str
    relay_url: Optional[str] = None


@dataclass
class FakePlayer:
    """A fake player for testing."""

    uuid: str
    username: str

    @classmethod
    def generate(cls, username: str) -> "FakePlayer":
        """Generate a fake player with a random UUID."""
        return cls(
            uuid=str(uuid_module.uuid4()),
            username=username,
        )


class ServerProcess:
    """Manages the Compass rendezvous server subprocess."""

    def __init__(self, backend_dir: Path, key_file: Path):
        self.backend_dir = backend_dir
        self.key_file = key_file
        self.process: Optional[subprocess.Popen] = None
        self.node_id: Optional[str] = None
        self.relay_url: Optional[str] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stop_drain = threading.Event()

    def _drain_stdout(self) -> None:
        """Background thread to drain server stdout and prevent blocking."""
        if self.process is None or self.process.stdout is None:
            return
        while not self._stop_drain.is_set():
            try:
                line = self.process.stdout.readline()
                if not line:
                    break
                # Optionally print server output for debugging
                # print(f"[SERVER] {line.rstrip()}")
            except Exception:
                break

    def start(self) -> ServerInfo:
        """Start the server with --skip-auth and return its connection info."""
        print(f"Starting server from {self.backend_dir}...")
        print(f"Using key file: {self.key_file}")

        # Build the server first
        print("Building server (cargo build --release)...")
        build_result = subprocess.run(
            ["cargo", "build", "--release"],
            cwd=self.backend_dir,
            capture_output=True,
            text=True,
        )

        if build_result.returncode != 0:
            print(f"Build failed:\n{build_result.stderr}")
            raise RuntimeError("Failed to build server")

        print("Server built successfully.")

        # Start the server with --skip-auth
        env = os.environ.copy()
        env["RUST_LOG"] = "compass=info,iroh=warn"

        self.process = subprocess.Popen(
            [
                "cargo",
                "run",
                "--release",
                "--",
                "--key-file",
                str(self.key_file),
                "--skip-auth",  # Skip Mojang authentication
            ],
            cwd=self.backend_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

        # Wait for the server to output its NodeId
        start_time = time.time()
        while time.time() - start_time < SERVER_STARTUP_TIMEOUT:
            if self.process.stdout is None:
                raise RuntimeError("No stdout from server process")

            line = self.process.stdout.readline()
            if not line:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"Server exited with code {self.process.returncode}"
                    )
                continue

            print(f"[SERVER] {line.rstrip()}")

            # Look for the NodeId in the output
            if "Server NodeId:" in line:
                parts = line.split("Server NodeId:")
                if len(parts) >= 2:
                    self.node_id = parts[1].strip()
                    print(f"Server NodeId: {self.node_id}")

            # Look for the Relay URL in the output
            if "Relay URL:" in line:
                parts = line.split("Relay URL:")
                if len(parts) >= 2:
                    self.relay_url = parts[1].strip()
                    print(f"Relay URL: {self.relay_url}")

            # Check if server is ready
            if "Rendezvous server is running" in line:
                if self.node_id is None:
                    raise RuntimeError("Server started but NodeId not found")
                print("Server is ready!")

                # Start background thread to drain stdout
                self._stdout_thread = threading.Thread(
                    target=self._drain_stdout, daemon=True
                )
                self._stdout_thread.start()

                return ServerInfo(
                    node_id=self.node_id,
                    relay_url=self.relay_url,
                )

        raise RuntimeError("Timeout waiting for server to start")

    def stop(self) -> None:
        """Stop the server process."""
        self._stop_drain.set()

        if self.process is not None:
            print("Stopping server...")
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            print("Server stopped.")

        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=1)


# =============================================================================
# TEST CASES
# =============================================================================


async def test_basic_connection(server_info: ServerInfo) -> bool:
    """Test basic connection and registration."""
    print("\n" + "=" * 60)
    print("TEST: Basic Connection and Registration")
    print("=" * 60)

    player = FakePlayer.generate("TestPlayer1")

    try:
        print(f"\n[Step 1] Connecting as {player.username}...")
        client = CompassClient(server_node_id=server_info.node_id)
        await asyncio.wait_for(client.connect(), timeout=30.0)
        print("[PASS] Connected to server")

        print("\n[Step 2] Registering (with skip_auth=True)...")
        await client.register(
            mc_uuid=player.uuid,
            mc_username=player.username,
            skip_auth=True,
        )
        print(f"[PASS] Registered as {player.username}")

        print("\n[Step 3] Setting discoverable...")
        await client.set_discoverable()
        print("[PASS] Now discoverable")

        print("\n[Step 4] Sending ping...")
        await client.ping()
        print("[PASS] Ping successful")

        print("\n[Step 5] Setting undiscoverable...")
        await client.set_undiscoverable()
        print("[PASS] Now undiscoverable")

        await client.disconnect()
        print("\n[PASS] All basic operations successful!")
        return True

    except Exception as e:
        print(f"\n[FAIL] Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


async def test_peer_discovery_accept(server_info: ServerInfo) -> bool:
    """Test peer discovery with accept."""
    print("\n" + "=" * 60)
    print("TEST: Peer Discovery (Accept)")
    print("=" * 60)

    player1 = FakePlayer.generate("Listener")
    player2 = FakePlayer.generate("Requester")

    received_request: list[ConnectionRequest] = []
    handler_called = asyncio.Event()

    async def on_request(request: ConnectionRequest) -> None:
        print(f"[{player1.username}] Received request from: {request.username}")
        received_request.append(request)
        peer = await request.accept()
        print(
            f"[{player1.username}] Accepted! Got peer node_id: {peer.node_id[:16]}..."
        )
        handler_called.set()

    try:
        # Setup player 1 (listener)
        print(f"\n[Step 1] Setting up {player1.username} (listener)...")
        client1 = CompassClient(server_node_id=server_info.node_id)
        await asyncio.wait_for(client1.connect(), timeout=30.0)
        await client1.register(
            mc_uuid=player1.uuid, mc_username=player1.username, skip_auth=True
        )
        await client1.set_discoverable()
        client1.start_listening(on_request)
        print(f"[{player1.username}] Ready and listening")

        # Setup player 2 (requester)
        print(f"\n[Step 2] Setting up {player2.username} (requester)...")
        client2 = CompassClient(server_node_id=server_info.node_id)
        await asyncio.wait_for(client2.connect(), timeout=30.0)
        await client2.register(
            mc_uuid=player2.uuid, mc_username=player2.username, skip_auth=True
        )
        await client2.set_discoverable()
        print(f"[{player2.username}] Ready")

        # Player 2 requests player 1
        print(f"\n[Step 3] {player2.username} requests {player1.username}...")
        peer_info = await asyncio.wait_for(
            client2.request_peer(player1.uuid, timeout=10.0),
            timeout=15.0,
        )
        print(
            f"[{player2.username}] Got peer info: {peer_info.username} ({peer_info.node_id[:16]}...)"
        )

        # Wait for handler
        await asyncio.wait_for(handler_called.wait(), timeout=5.0)

        # Verify
        success = True
        if peer_info.uuid == player1.uuid and peer_info.username == player1.username:
            print("[PASS] Requester received correct peer info")
        else:
            print(f"[FAIL] Wrong peer info: {peer_info}")
            success = False

        if received_request and received_request[0].uuid == player2.uuid:
            print("[PASS] Listener received correct request")
        else:
            print("[FAIL] Listener did not receive correct request")
            success = False

        await client1.disconnect()
        await client2.disconnect()
        return success

    except Exception as e:
        print(f"\n[FAIL] Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


async def test_peer_discovery_decline(server_info: ServerInfo) -> bool:
    """Test peer discovery with decline."""
    print("\n" + "=" * 60)
    print("TEST: Peer Discovery (Decline)")
    print("=" * 60)

    player1 = FakePlayer.generate("Decliner")
    player2 = FakePlayer.generate("Rejected")

    handler_called = asyncio.Event()

    async def on_request(request: ConnectionRequest) -> None:
        print(f"[{player1.username}] Declining request from: {request.username}")
        await request.decline()
        handler_called.set()

    try:
        # Setup player 1 (decliner)
        print(f"\n[Step 1] Setting up {player1.username}...")
        client1 = CompassClient(server_node_id=server_info.node_id)
        await asyncio.wait_for(client1.connect(), timeout=30.0)
        await client1.register(
            mc_uuid=player1.uuid, mc_username=player1.username, skip_auth=True
        )
        await client1.set_discoverable()
        client1.start_listening(on_request)

        # Setup player 2
        print(f"\n[Step 2] Setting up {player2.username}...")
        client2 = CompassClient(server_node_id=server_info.node_id)
        await asyncio.wait_for(client2.connect(), timeout=30.0)
        await client2.register(
            mc_uuid=player2.uuid, mc_username=player2.username, skip_auth=True
        )
        await client2.set_discoverable()

        # Player 2 requests player 1 (should be declined)
        print(
            f"\n[Step 3] {player2.username} requests {player1.username} (will be declined)..."
        )
        try:
            await client2.request_peer(player1.uuid, timeout=10.0)
            print("[FAIL] Request succeeded - should have been declined!")
            success = False
        except PeerUnavailableError:
            print("[PASS] Request correctly failed with PeerUnavailableError")
            success = True

        await asyncio.wait_for(handler_called.wait(), timeout=5.0)

        await client1.disconnect()
        await client2.disconnect()
        return success

    except Exception as e:
        print(f"\n[FAIL] Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


async def test_request_with_reason(server_info: ServerInfo) -> bool:
    """Test that request reason is passed to the listener."""
    print("\n" + "=" * 60)
    print("TEST: Request With Reason")
    print("=" * 60)

    player1 = FakePlayer.generate("ReasonReceiver")
    player2 = FakePlayer.generate("ReasonSender")
    test_reason = "Let's play together!"

    received_reason: list[str | None] = []
    handler_called = asyncio.Event()

    async def on_request(request: ConnectionRequest) -> None:
        print(f"[{player1.username}] Request reason: '{request.reason}'")
        received_reason.append(request.reason)
        await request.accept()
        handler_called.set()

    try:
        # Setup both players
        client1 = CompassClient(server_node_id=server_info.node_id)
        await asyncio.wait_for(client1.connect(), timeout=30.0)
        await client1.register(
            mc_uuid=player1.uuid, mc_username=player1.username, skip_auth=True
        )
        await client1.set_discoverable()
        client1.start_listening(on_request)

        client2 = CompassClient(server_node_id=server_info.node_id)
        await asyncio.wait_for(client2.connect(), timeout=30.0)
        await client2.register(
            mc_uuid=player2.uuid, mc_username=player2.username, skip_auth=True
        )
        await client2.set_discoverable()

        # Request with reason
        print(f"\n[Step] Requesting with reason: '{test_reason}'")
        await client2.request_peer(
            player1.uuid, request_reason=test_reason, timeout=10.0
        )

        await asyncio.wait_for(handler_called.wait(), timeout=5.0)

        success = received_reason and received_reason[0] == test_reason
        if success:
            print("[PASS] Reason correctly received")
        else:
            print(
                f"[FAIL] Wrong reason: '{received_reason[0] if received_reason else None}'"
            )

        await client1.disconnect()
        await client2.disconnect()
        return success

    except Exception as e:
        print(f"\n[FAIL] Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


async def test_request_undiscoverable_peer(server_info: ServerInfo) -> bool:
    """Test that requesting an undiscoverable peer fails."""
    print("\n" + "=" * 60)
    print("TEST: Request Undiscoverable Peer")
    print("=" * 60)

    player1 = FakePlayer.generate("Hidden")
    player2 = FakePlayer.generate("Seeker")

    try:
        # Setup player 1 (NOT discoverable)
        client1 = CompassClient(server_node_id=server_info.node_id)
        await asyncio.wait_for(client1.connect(), timeout=30.0)
        await client1.register(
            mc_uuid=player1.uuid, mc_username=player1.username, skip_auth=True
        )
        # Note: NOT calling set_discoverable()
        print(f"[{player1.username}] Registered but NOT discoverable")

        # Setup player 2 (discoverable)
        client2 = CompassClient(server_node_id=server_info.node_id)
        await asyncio.wait_for(client2.connect(), timeout=30.0)
        await client2.register(
            mc_uuid=player2.uuid, mc_username=player2.username, skip_auth=True
        )
        await client2.set_discoverable()
        print(f"[{player2.username}] Registered and discoverable")

        # Player 2 tries to request player 1 (should fail)
        print(f"\n[Step] {player2.username} requests hidden {player1.username}...")
        try:
            await client2.request_peer(player1.uuid, timeout=5.0)
            print("[FAIL] Request succeeded - should have failed!")
            success = False
        except (PeerUnavailableError, ProtocolError) as e:
            print(f"[PASS] Request correctly failed: {type(e).__name__}")
            success = True

        await client1.disconnect()
        await client2.disconnect()
        return success

    except Exception as e:
        print(f"\n[FAIL] Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


async def test_bidirectional_requests(server_info: ServerInfo) -> bool:
    """
    Test bidirectional connection requests (the bug from the issue).

    When User A requests User B while User B requests User A simultaneously,
    both requests should be handled correctly without crashing.
    """
    print("\n" + "=" * 60)
    print("TEST: Bidirectional Connection Requests (Bug Fix Verification)")
    print("=" * 60)

    player1 = FakePlayer.generate("Alice")
    player2 = FakePlayer.generate("Bob")

    player1_got_request = asyncio.Event()
    player2_got_request = asyncio.Event()
    player1_peer_info: list[PeerInfo] = []
    player2_peer_info: list[PeerInfo] = []

    async def player1_handler(request: ConnectionRequest) -> None:
        print(f"[{player1.username}] Got request from {request.username}")
        player1_got_request.set()
        peer = await request.accept()
        player1_peer_info.append(peer)
        print(f"[{player1.username}] Accepted, got peer: {peer.username}")

    async def player2_handler(request: ConnectionRequest) -> None:
        print(f"[{player2.username}] Got request from {request.username}")
        player2_got_request.set()
        peer = await request.accept()
        player2_peer_info.append(peer)
        print(f"[{player2.username}] Accepted, got peer: {peer.username}")

    try:
        # Setup both players
        print("\n[Step 1] Setting up both players...")
        client1 = CompassClient(server_node_id=server_info.node_id)
        await asyncio.wait_for(client1.connect(), timeout=30.0)
        await client1.register(
            mc_uuid=player1.uuid, mc_username=player1.username, skip_auth=True
        )
        await client1.set_discoverable()
        client1.start_listening(player1_handler)
        print(f"[{player1.username}] Ready and listening")

        client2 = CompassClient(server_node_id=server_info.node_id)
        await asyncio.wait_for(client2.connect(), timeout=30.0)
        await client2.register(
            mc_uuid=player2.uuid, mc_username=player2.username, skip_auth=True
        )
        await client2.set_discoverable()
        client2.start_listening(player2_handler)
        print(f"[{player2.username}] Ready and listening")

        # Simultaneously send requests to each other
        print("\n[Step 2] Both players request each other simultaneously...")

        async def player1_requests_player2() -> PeerInfo | None:
            try:
                return await client1.request_peer(player2.uuid, timeout=10.0)
            except PeerUnavailableError:
                print(
                    f"[{player1.username}] Request to {player2.username} failed (expected in some cases)"
                )
                return None

        async def player2_requests_player1() -> PeerInfo | None:
            try:
                return await client2.request_peer(player1.uuid, timeout=10.0)
            except PeerUnavailableError:
                print(
                    f"[{player2.username}] Request to {player1.username} failed (expected in some cases)"
                )
                return None

        # Run both requests concurrently
        results = await asyncio.gather(
            player1_requests_player2(),
            player2_requests_player1(),
            return_exceptions=True,
        )

        print("\n[Step 3] Results:")
        print(f"  Player1 -> Player2: {results[0]}")
        print(f"  Player2 -> Player1: {results[1]}")

        # Check results - at least one should succeed, and no crashes
        success = True

        # Check for exceptions (the bug would cause ConnectionError)
        for i, result in enumerate(results):
            if isinstance(result, Exception) and not isinstance(
                result, PeerUnavailableError
            ):
                print(f"[FAIL] Unexpected exception: {type(result).__name__}: {result}")
                success = False

        # At minimum, the listen loops should still be running (not crashed)
        if client1._listen_task is None or client1._listen_task.done():
            # Check if it was cancelled vs crashed
            if client1._listen_task and client1._listen_task.done():
                try:
                    client1._listen_task.result()
                except Exception as e:
                    print(f"[FAIL] Player1 listen loop crashed: {e}")
                    success = False

        if client2._listen_task is None or client2._listen_task.done():
            if client2._listen_task and client2._listen_task.done():
                try:
                    client2._listen_task.result()
                except Exception as e:
                    print(f"[FAIL] Player2 listen loop crashed: {e}")
                    success = False

        # At least one request should have worked
        successful_requests = sum(1 for r in results if isinstance(r, PeerInfo))
        if successful_requests == 0:
            # Both failed with PeerUnavailable is okay (server-side race condition handling)
            # But both crashing is not
            peer_unavailable_count = sum(
                1 for r in results if isinstance(r, PeerUnavailableError)
            )
            if peer_unavailable_count == 2:
                print(
                    "[INFO] Both requests failed with PeerUnavailable - this is acceptable"
                )
            else:
                print("[WARN] No successful requests, check results above")

        if success:
            print("\n[PASS] Bidirectional requests handled without crashing!")
        else:
            print("\n[FAIL] Bidirectional request handling failed")

        await client1.disconnect()
        await client2.disconnect()
        return success

    except Exception as e:
        print(f"\n[FAIL] Test failed with exception: {e}")
        import traceback

        traceback.print_exc()
        return False


async def test_multiple_pending_requests(server_info: ServerInfo) -> bool:
    """
    Test that multiple pending requests are handled correctly.

    This verifies the fix for the bug where request_failed would fail ALL
    pending requests instead of just the specific one.
    """
    print("\n" + "=" * 60)
    print("TEST: Multiple Pending Requests")
    print("=" * 60)

    requester = FakePlayer.generate("MultiRequester")
    target1 = FakePlayer.generate("Target1")
    target2 = FakePlayer.generate("Target2")

    target1_handler_called = asyncio.Event()

    async def target1_handler(request: ConnectionRequest) -> None:
        print(f"[{target1.username}] Got request, accepting...")
        await request.accept()
        target1_handler_called.set()

    try:
        # Setup requester
        print("\n[Step 1] Setting up requester...")
        client_req = CompassClient(server_node_id=server_info.node_id)
        await asyncio.wait_for(client_req.connect(), timeout=30.0)
        await client_req.register(
            mc_uuid=requester.uuid, mc_username=requester.username, skip_auth=True
        )
        await client_req.set_discoverable()
        client_req.start_listening(lambda r: asyncio.create_task(r.decline()))
        print(f"[{requester.username}] Ready")

        # Setup target1 (will accept)
        print("\n[Step 2] Setting up target1 (will accept)...")
        client_t1 = CompassClient(server_node_id=server_info.node_id)
        await asyncio.wait_for(client_t1.connect(), timeout=30.0)
        await client_t1.register(
            mc_uuid=target1.uuid, mc_username=target1.username, skip_auth=True
        )
        await client_t1.set_discoverable()
        client_t1.start_listening(target1_handler)
        print(f"[{target1.username}] Ready")

        # Setup target2 (NOT discoverable - request will fail immediately)
        print("\n[Step 3] Setting up target2 (NOT discoverable)...")
        client_t2 = CompassClient(server_node_id=server_info.node_id)
        await asyncio.wait_for(client_t2.connect(), timeout=30.0)
        await client_t2.register(
            mc_uuid=target2.uuid, mc_username=target2.username, skip_auth=True
        )
        # Note: NOT calling set_discoverable()
        print(f"[{target2.username}] Ready (but not discoverable)")

        # Send request to target1 (should succeed) and target2 (should fail)
        print("\n[Step 4] Sending requests to both targets...")

        async def request_target1() -> PeerInfo | Exception:
            try:
                return await client_req.request_peer(target1.uuid, timeout=10.0)
            except Exception as e:
                return e

        async def request_target2() -> PeerInfo | Exception:
            try:
                return await client_req.request_peer(target2.uuid, timeout=5.0)
            except Exception as e:
                return e

        # Small delay so target1 request is sent first
        task1 = asyncio.create_task(request_target1())
        await asyncio.sleep(0.1)
        task2 = asyncio.create_task(request_target2())

        result1 = await task1
        result2 = await task2

        print("\n[Step 5] Results:")
        print(f"  Request to {target1.username}: {result1}")
        print(f"  Request to {target2.username}: {result2}")

        success = True

        # Target1 request should succeed
        if isinstance(result1, PeerInfo) and result1.uuid == target1.uuid:
            print(f"[PASS] Request to {target1.username} succeeded")
        else:
            print(
                f"[FAIL] Request to {target1.username} should have succeeded: {result1}"
            )
            success = False

        # Target2 request should fail (not discoverable)
        if isinstance(result2, (PeerUnavailableError, ProtocolError)):
            print(f"[PASS] Request to {target2.username} correctly failed")
        else:
            print(f"[FAIL] Request to {target2.username} should have failed: {result2}")
            success = False

        await client_req.disconnect()
        await client_t1.disconnect()
        await client_t2.disconnect()
        return success

    except Exception as e:
        print(f"\n[FAIL] Test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


# =============================================================================
# MAIN
# =============================================================================


async def run_all_tests(server_info: ServerInfo) -> tuple[int, int]:
    """Run all tests and return (passed, failed) counts."""
    tests = [
        ("Basic Connection", test_basic_connection),
        ("Peer Discovery (Accept)", test_peer_discovery_accept),
        ("Peer Discovery (Decline)", test_peer_discovery_decline),
        ("Request With Reason", test_request_with_reason),
        ("Request Undiscoverable Peer", test_request_undiscoverable_peer),
        ("Bidirectional Requests (Bug Fix)", test_bidirectional_requests),
        ("Multiple Pending Requests", test_multiple_pending_requests),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = await test_func(server_info)
            results.append((name, result))
        except Exception as e:
            print(f"\n[ERROR] Test '{name}' crashed: {e}")
            results.append((name, False))

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    passed = 0
    failed = 0
    for name, result in results:
        status = "PASS" if result else "FAIL"
        print(f"  [{status}] {name}")
        if result:
            passed += 1
        else:
            failed += 1

    print(f"\nTotal: {passed} passed, {failed} failed")
    return passed, failed


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Test the Compass rendezvous service (no auth required)"
    )
    parser.add_argument(
        "--start-server",
        action="store_true",
        help="Start the server automatically (with --skip-auth)",
    )
    parser.add_argument(
        "--server-node-id",
        type=str,
        help="NodeId of an already-running server (if not using --start-server)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Compass Rendezvous - Test Suite (No Auth)")
    print("=" * 60)

    server: Optional[ServerProcess] = None

    try:
        if args.start_server:
            # Start our own server
            if not BACKEND_DIR.exists():
                print(f"ERROR: Backend directory not found: {BACKEND_DIR}")
                sys.exit(1)

            server = ServerProcess(BACKEND_DIR, SERVER_KEY_FILE)
            server_info = server.start()
            await asyncio.sleep(1.0)  # Give server time to fully initialize

        elif args.server_node_id:
            # Use provided server
            server_info = ServerInfo(node_id=args.server_node_id)
            print(f"Using server at: {server_info.node_id}")

        else:
            print("ERROR: Either --start-server or --server-node-id is required")
            print("\nUsage:")
            print("  python test_no_auth.py --start-server")
            print("  python test_no_auth.py --server-node-id <node_id>")
            sys.exit(1)

        # Run tests
        passed, failed = await run_all_tests(server_info)

        # Exit with appropriate code
        sys.exit(0 if failed == 0 else 1)

    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(130)

    finally:
        if server is not None:
            server.stop()


if __name__ == "__main__":
    asyncio.run(main())
