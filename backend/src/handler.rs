//! Rendezvous protocol handler.
//!
//! This module implements the `ProtocolHandler` trait for handling
//! incoming iroh connections and processing the rendezvous protocol.

use std::sync::Arc;
use std::time::Instant;

use anyhow::Result;
use futures_lite::future::Boxed as BoxFuture;
use iroh::endpoint::Connection;
use iroh::protocol::ProtocolHandler;
use iroh::PublicKey;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::sync::{mpsc, oneshot};
use tracing::{debug, error, info, warn};

use crate::mojang::{generate_server_id, MojangClient};
use crate::protocol::{ClientMessage, ErrorReason, ServerMessage};
use crate::state::{
    ConnectionRequestNotification, ConnectionResult, PeerNotification, PendingChallenge,
    PendingRequest, Session, SessionNotification, State,
};

/// Generate a random connection nonce.
fn generate_conn_nonce() -> String {
    use rand::RngCore;

    let mut bytes = [0u8; 16];
    rand::thread_rng().fill_bytes(&mut bytes);
    base16ct::lower::encode_string(&bytes)
}

/// The rendezvous protocol handler.
#[derive(Debug, Clone)]
pub struct RendezvousHandler {
    state: Arc<State>,
    mojang: MojangClient,
}

impl RendezvousHandler {
    /// Create a new rendezvous handler.
    pub fn new(state: Arc<State>) -> Self {
        Self {
            state,
            mojang: MojangClient::new(),
        }
    }

    /// Handle a single client connection.
    async fn handle_connection(&self, conn: Connection) -> Result<()> {
        let remote_node_id = conn.remote_node_id()?;
        info!(?remote_node_id, "New connection");

        // Accept a bidirectional stream for the protocol
        debug!(?remote_node_id, "Waiting for bi-stream...");
        let (send, recv) = conn.accept_bi().await?;
        debug!(?remote_node_id, "Bi-stream accepted");
        let mut writer = send;
        let mut reader = BufReader::new(recv);

        // Read and discard the init message that triggered the stream
        let mut init_line = String::new();
        reader.read_line(&mut init_line).await?;
        debug!(?remote_node_id, "Received init message");

        // Generate and send the connection nonce
        let conn_nonce = generate_conn_nonce();
        let hello = ServerMessage::Hello {
            conn_nonce: conn_nonce.clone(),
        };
        debug!(?remote_node_id, "Sending HELLO message...");
        self.send_message(&mut writer, &hello).await?;
        debug!(?remote_node_id, "Sent HELLO");

        // Create notification channel for this session
        let (notify_tx, mut notify_rx) = mpsc::channel::<SessionNotification>(16);

        // Process messages until the connection closes
        let mut line = String::new();
        loop {
            tokio::select! {
                // Handle incoming notifications
                Some(notification) = notify_rx.recv() => {
                    let response = match notification {
                        SessionNotification::ConnectionRequest(req) => {
                            ServerMessage::ConnectionRequest {
                                uuid: req.from_uuid,
                                username: req.from_username,
                                reason: req.reason,
                            }
                        }
                        SessionNotification::Peer(peer) => {
                            ServerMessage::Peer {
                                uuid: peer.mc_uuid,
                                username: peer.mc_username,
                                node_id: peer.node_id.to_string(),
                            }
                        }
                    };
                    if let Err(e) = self.send_message(&mut writer, &response).await {
                        warn!(?remote_node_id, "Failed to send notification: {}", e);
                        break;
                    }
                }

                // Handle incoming messages from client
                result = reader.read_line(&mut line) => {
                    match result {
                        Ok(0) => {
                            debug!(?remote_node_id, "Connection closed");
                            break;
                        }
                        Ok(_) => {
                            let trimmed = line.trim();
                            if trimmed.is_empty() {
                                line.clear();
                                continue;
                            }

                            match serde_json::from_str::<ClientMessage>(trimmed) {
                                Ok(msg) => {
                                    if let Err(e) = self
                                        .handle_message(
                                            &remote_node_id,
                                            &conn_nonce,
                                            msg,
                                            &mut writer,
                                            notify_tx.clone(),
                                        )
                                        .await
                                    {
                                        warn!(?remote_node_id, "Error handling message: {}", e);
                                        break;
                                    }
                                }
                                Err(e) => {
                                    warn!(?remote_node_id, "Invalid message: {}", e);
                                    let error = ServerMessage::Error {
                                        reason: ErrorReason::InvalidMessage,
                                    };
                                    let _ = self.send_message(&mut writer, &error).await;
                                }
                            }
                            line.clear();
                        }
                        Err(e) => {
                            error!(?remote_node_id, "Read error: {}", e);
                            break;
                        }
                    }
                }
            }
        }

        // Clean up session on disconnect
        self.state.remove_session_by_node(&remote_node_id);
        info!(?remote_node_id, "Connection handler finished");

        Ok(())
    }

    /// Handle a single client message.
    async fn handle_message(
        &self,
        node_id: &PublicKey,
        expected_nonce: &str,
        msg: ClientMessage,
        writer: &mut iroh::endpoint::SendStream,
        notify_tx: mpsc::Sender<SessionNotification>,
    ) -> Result<()> {
        // Extract and validate nonce from message
        let msg_nonce = match &msg {
            ClientMessage::Register { conn_nonce, .. } => conn_nonce,
            ClientMessage::Verify { conn_nonce } => conn_nonce,
            ClientMessage::Discoverable { conn_nonce } => conn_nonce,
            ClientMessage::Undiscoverable { conn_nonce } => conn_nonce,
            ClientMessage::Connect { conn_nonce, .. } => conn_nonce,
            ClientMessage::Accept { conn_nonce, .. } => conn_nonce,
            ClientMessage::Decline { conn_nonce, .. } => conn_nonce,
            ClientMessage::Ping { conn_nonce } => conn_nonce,
        };

        if msg_nonce != expected_nonce {
            let error = ServerMessage::Error {
                reason: ErrorReason::InvalidNonce,
            };
            self.send_message(writer, &error).await?;
            anyhow::bail!("invalid nonce");
        }

        match msg {
            ClientMessage::Register {
                mc_uuid,
                mc_username,
                ..
            } => {
                self.handle_register(node_id, mc_uuid, mc_username, writer, notify_tx)
                    .await
            }
            ClientMessage::Verify { .. } => self.handle_verify(node_id, writer).await,
            ClientMessage::Discoverable { .. } => self.handle_discoverable(node_id, writer).await,
            ClientMessage::Undiscoverable { .. } => {
                self.handle_undiscoverable(node_id, writer).await
            }
            ClientMessage::Connect {
                to_uuid, reason, ..
            } => self.handle_connect(node_id, to_uuid, reason, writer).await,
            ClientMessage::Accept { from_uuid, .. } => {
                self.handle_accept(node_id, from_uuid, writer).await
            }
            ClientMessage::Decline { from_uuid, .. } => {
                self.handle_decline(node_id, from_uuid, writer).await
            }
            ClientMessage::Ping { .. } => self.handle_ping(node_id, writer).await,
        }
    }

    /// Handle a register message.
    async fn handle_register(
        &self,
        node_id: &PublicKey,
        mc_uuid: String,
        mc_username: String,
        writer: &mut iroh::endpoint::SendStream,
        notify_tx: mpsc::Sender<SessionNotification>,
    ) -> Result<()> {
        debug!(?node_id, %mc_uuid, %mc_username, "Register request");

        // Generate challenge and store with notify_tx for later session creation
        // Note: If a session already exists, it will be replaced after verification
        let server_id = generate_server_id();
        let challenge = PendingChallenge {
            node_id: *node_id,
            mc_uuid,
            mc_username,
            server_id: server_id.clone(),
            created_at: Instant::now(),
            notify_tx,
        };

        self.state.insert_challenge(challenge);

        let response = ServerMessage::Challenge { server_id };
        self.send_message(writer, &response).await?;

        debug!(?node_id, "Sent challenge");
        Ok(())
    }

    /// Handle a verify message.
    async fn handle_verify(
        &self,
        node_id: &PublicKey,
        writer: &mut iroh::endpoint::SendStream,
    ) -> Result<()> {
        debug!(?node_id, "Verify request");

        // Get the pending challenge
        let challenge = match self.state.take_challenge(node_id) {
            Some(c) => c,
            None => {
                let error = ServerMessage::Error {
                    reason: ErrorReason::NotRegistered,
                };
                self.send_message(writer, &error).await?;
                return Ok(());
            }
        };

        // Verify with Mojang
        let verified = self
            .mojang
            .verify_player(
                &challenge.mc_username,
                &challenge.mc_uuid,
                &challenge.server_id,
            )
            .await;

        match verified {
            Ok(_) => {
                // Create session with the notify_tx from the challenge
                let session = Session {
                    node_id: *node_id,
                    mc_uuid: challenge.mc_uuid.clone(),
                    mc_username: challenge.mc_username.clone(),
                    registered_at: Instant::now(),
                    last_seen: Instant::now(),
                    discoverable: false,
                    notify_tx: challenge.notify_tx,
                };

                // Insert or replace the session (replaces any existing session for this UUID)
                self.state.upsert_session(session);

                info!(
                    ?node_id,
                    mc_uuid = %challenge.mc_uuid,
                    mc_username = %challenge.mc_username,
                    "Session created"
                );

                let response = ServerMessage::Registered;
                self.send_message(writer, &response).await?;
            }
            Err(e) => {
                warn!(?node_id, "Mojang verification failed: {}", e);
                let error = ServerMessage::Error {
                    reason: ErrorReason::VerificationFailed,
                };
                self.send_message(writer, &error).await?;
            }
        }

        Ok(())
    }

    /// Handle a discoverable message.
    async fn handle_discoverable(
        &self,
        node_id: &PublicKey,
        writer: &mut iroh::endpoint::SendStream,
    ) -> Result<()> {
        debug!(?node_id, "Discoverable request");

        let session = match self.state.get_session_by_node(node_id) {
            Some(s) => s,
            None => {
                let error = ServerMessage::Error {
                    reason: ErrorReason::NotRegistered,
                };
                self.send_message(writer, &error).await?;
                return Ok(());
            }
        };

        self.state.set_discoverable(&session.mc_uuid, true);
        info!(?node_id, mc_uuid = %session.mc_uuid, "Session now discoverable");

        let response = ServerMessage::DiscoverableAck;
        self.send_message(writer, &response).await?;

        Ok(())
    }

    /// Handle an undiscoverable message.
    async fn handle_undiscoverable(
        &self,
        node_id: &PublicKey,
        writer: &mut iroh::endpoint::SendStream,
    ) -> Result<()> {
        debug!(?node_id, "Undiscoverable request");

        let session = match self.state.get_session_by_node(node_id) {
            Some(s) => s,
            None => {
                let error = ServerMessage::Error {
                    reason: ErrorReason::NotRegistered,
                };
                self.send_message(writer, &error).await?;
                return Ok(());
            }
        };

        self.state.set_discoverable(&session.mc_uuid, false);
        info!(?node_id, mc_uuid = %session.mc_uuid, "Session now undiscoverable");

        let response = ServerMessage::UndiscoverableAck;
        self.send_message(writer, &response).await?;

        Ok(())
    }

    /// Handle a connect message.
    ///
    /// This sends a connection request to the target peer. The request is pending
    /// until the target accepts or declines (or times out).
    async fn handle_connect(
        &self,
        node_id: &PublicKey,
        to_uuid: String,
        reason: Option<String>,
        writer: &mut iroh::endpoint::SendStream,
    ) -> Result<()> {
        debug!(?node_id, %to_uuid, ?reason, "Connect request");

        // Get our session
        let session = match self.state.get_session_by_node(node_id) {
            Some(s) => s,
            None => {
                let error = ServerMessage::Error {
                    reason: ErrorReason::NotRegistered,
                };
                self.send_message(writer, &error).await?;
                return Ok(());
            }
        };

        // Check if we're discoverable
        if !session.discoverable {
            let error = ServerMessage::Error {
                reason: ErrorReason::NotDiscoverable,
            };
            self.send_message(writer, &error).await?;
            return Ok(());
        }

        // Check if target exists and is discoverable
        // For privacy, we return the same error whether target doesn't exist or isn't discoverable
        let target = match self.state.get_session(&to_uuid) {
            Some(s) if s.discoverable => s,
            _ => {
                let error = ServerMessage::Error {
                    reason: ErrorReason::PeerUnavailable,
                };
                self.send_message(writer, &error).await?;
                return Ok(());
            }
        };

        // Create a channel for receiving the result
        let (result_tx, result_rx) = oneshot::channel();

        // Create pending request
        let pending = PendingRequest {
            from_uuid: session.mc_uuid.clone(),
            from_username: session.mc_username.clone(),
            from_node_id: session.node_id,
            to_uuid: to_uuid.clone(),
            reason: reason.clone(),
            created_at: Instant::now(),
            result_tx,
        };

        // Store the pending request
        if !self.state.insert_pending_request(pending) {
            // A request already exists from this user to this target
            // Just wait for that one
            debug!(
                from_uuid = %session.mc_uuid,
                to_uuid = %to_uuid,
                "Duplicate request - ignoring"
            );
        }

        // Send notification to the target peer
        let notification = SessionNotification::ConnectionRequest(ConnectionRequestNotification {
            from_uuid: session.mc_uuid.clone(),
            from_username: session.mc_username.clone(),
            reason,
        });

        // Best effort - if channel is full or closed, the request will timeout
        let _ = target.notify_tx.try_send(notification);

        info!(
            from_uuid = %session.mc_uuid,
            to_uuid = %to_uuid,
            "Connection request sent, waiting for response"
        );

        // Send acknowledgment that request was sent
        let response = ServerMessage::RequestSent;
        self.send_message(writer, &response).await?;

        // Wait for the result (accept/decline/timeout)
        // The result will be sent by handle_accept or handle_decline, or the cleanup task
        match result_rx.await {
            Ok(ConnectionResult::Accepted(peer)) => {
                // Send the peer info to the requester
                let response = ServerMessage::Peer {
                    uuid: peer.mc_uuid,
                    username: peer.mc_username,
                    node_id: peer.node_id.to_string(),
                };
                self.send_message(writer, &response).await?;
            }
            Ok(ConnectionResult::Declined) | Err(_) => {
                // Declined or channel closed (timeout) - send generic failure
                let response = ServerMessage::RequestFailed;
                self.send_message(writer, &response).await?;
            }
        }

        Ok(())
    }

    /// Handle an accept message.
    ///
    /// This accepts a pending connection request from another peer.
    /// Both parties receive each other's full info (including node_id).
    async fn handle_accept(
        &self,
        node_id: &PublicKey,
        from_uuid: String,
        writer: &mut iroh::endpoint::SendStream,
    ) -> Result<()> {
        debug!(?node_id, %from_uuid, "Accept request");

        // Get our session
        let session = match self.state.get_session_by_node(node_id) {
            Some(s) => s,
            None => {
                let error = ServerMessage::Error {
                    reason: ErrorReason::NotRegistered,
                };
                self.send_message(writer, &error).await?;
                return Ok(());
            }
        };

        // Get the pending request
        let pending = match self
            .state
            .take_pending_request(&from_uuid, &session.mc_uuid)
        {
            Some(p) => p,
            None => {
                let error = ServerMessage::Error {
                    reason: ErrorReason::NoPendingRequest,
                };
                self.send_message(writer, &error).await?;
                return Ok(());
            }
        };

        info!(
            from_uuid = %pending.from_uuid,
            to_uuid = %session.mc_uuid,
            "Connection accepted"
        );

        // Send our info to the requester via their result channel
        let our_info = PeerNotification {
            mc_uuid: session.mc_uuid.clone(),
            mc_username: session.mc_username.clone(),
            node_id: session.node_id,
        };
        let _ = pending.result_tx.send(ConnectionResult::Accepted(our_info));

        // Send the requester's info to us via notification channel
        // (This will be picked up by the connection loop and sent to the client)
        let their_info = PeerNotification {
            mc_uuid: pending.from_uuid.clone(),
            mc_username: pending.from_username.clone(),
            node_id: pending.from_node_id,
        };
        let _ = session
            .notify_tx
            .try_send(SessionNotification::Peer(their_info));

        // Send acknowledgment
        let response = ServerMessage::AcceptAck;
        self.send_message(writer, &response).await?;

        Ok(())
    }

    /// Handle a decline message.
    ///
    /// This declines a pending connection request from another peer.
    async fn handle_decline(
        &self,
        node_id: &PublicKey,
        from_uuid: String,
        writer: &mut iroh::endpoint::SendStream,
    ) -> Result<()> {
        debug!(?node_id, %from_uuid, "Decline request");

        // Get our session
        let session = match self.state.get_session_by_node(node_id) {
            Some(s) => s,
            None => {
                let error = ServerMessage::Error {
                    reason: ErrorReason::NotRegistered,
                };
                self.send_message(writer, &error).await?;
                return Ok(());
            }
        };

        // Get the pending request
        let pending = match self
            .state
            .take_pending_request(&from_uuid, &session.mc_uuid)
        {
            Some(p) => p,
            None => {
                let error = ServerMessage::Error {
                    reason: ErrorReason::NoPendingRequest,
                };
                self.send_message(writer, &error).await?;
                return Ok(());
            }
        };

        info!(
            from_uuid = %pending.from_uuid,
            to_uuid = %session.mc_uuid,
            "Connection declined"
        );

        // Send declined result to the requester
        let _ = pending.result_tx.send(ConnectionResult::Declined);

        // Send acknowledgment
        let response = ServerMessage::DeclineAck;
        self.send_message(writer, &response).await?;

        Ok(())
    }

    /// Handle a ping message.
    async fn handle_ping(
        &self,
        node_id: &PublicKey,
        writer: &mut iroh::endpoint::SendStream,
    ) -> Result<()> {
        // Update last_seen if registered
        if let Some(session) = self.state.get_session_by_node(node_id) {
            self.state.touch_session(&session.mc_uuid);
        }

        let response = ServerMessage::Pong;
        self.send_message(writer, &response).await?;

        Ok(())
    }

    /// Send a message to the client.
    async fn send_message(
        &self,
        writer: &mut iroh::endpoint::SendStream,
        msg: &ServerMessage,
    ) -> Result<()> {
        let json = serde_json::to_string(msg)?;
        writer.write_all(json.as_bytes()).await?;
        writer.write_all(b"\n").await?;
        writer.flush().await?;
        Ok(())
    }
}

impl ProtocolHandler for RendezvousHandler {
    fn accept(&self, conn: Connection) -> BoxFuture<Result<()>> {
        let this = self.clone();
        Box::pin(async move { this.handle_connection(conn).await })
    }
}
