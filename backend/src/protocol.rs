//! Protocol message types for the rendezvous service.
//!
//! All messages are JSON over iroh ALPN protocol `rendezvous/1`.

use serde::{Deserialize, Serialize};

/// The ALPN protocol identifier for the rendezvous service.
pub const ALPN: &[u8] = b"rendezvous/1";

/// Messages sent from the client to the server.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ClientMessage {
    /// Register a Minecraft identity with the rendezvous server.
    Register {
        mc_uuid: String,
        mc_username: String,
        conn_nonce: String,
    },
    /// Verify Minecraft ownership after completing Mojang session join.
    Verify { conn_nonce: String },
    /// Request to become discoverable by other peers.
    Discoverable { conn_nonce: String },
    /// Request to become undiscoverable by other peers.
    Undiscoverable { conn_nonce: String },
    /// Request to connect to another peer.
    /// The requester will wait until the target accepts or declines (or times out).
    Connect {
        to_uuid: String,
        conn_nonce: String,
        /// Optional reason for the connection request.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        reason: Option<String>,
    },
    /// Accept a pending connection request from another peer.
    Accept {
        from_uuid: String,
        conn_nonce: String,
    },
    /// Decline a pending connection request from another peer.
    Decline {
        from_uuid: String,
        conn_nonce: String,
    },
    /// Heartbeat to keep the session alive.
    Ping { conn_nonce: String },
}

/// Messages sent from the server to the client.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ServerMessage {
    /// Initial message sent after connection, containing the connection nonce.
    Hello { conn_nonce: String },
    /// Challenge for Minecraft authentication.
    Challenge { server_id: String },
    /// Registration successful.
    Registered,
    /// Discoverability enabled.
    DiscoverableAck,
    /// Discoverability disabled.
    UndiscoverableAck,
    /// Incoming connection request notification.
    /// Contains only uuid/username - node_id is NOT shared until accepted.
    ConnectionRequest {
        uuid: String,
        username: String,
        /// The reason provided by the requester.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        reason: Option<String>,
    },
    /// Acknowledgment that connection request was sent and is pending.
    RequestSent,
    /// Connection request was accepted - acknowledgment to the accepter.
    AcceptAck,
    /// Connection request was declined - acknowledgment to the decliner.
    DeclineAck,
    /// Peer information after connection is accepted.
    /// Both parties receive this with the other's full info.
    Peer {
        uuid: String,
        username: String,
        node_id: String,
    },
    /// Connection request was declined or failed (generic for privacy).
    /// Includes the target UUID so clients can match this to pending requests.
    RequestFailed {
        /// The UUID of the peer that was requested (the target of our connect).
        target_uuid: String,
    },
    /// Heartbeat response.
    Pong,
    /// Error response.
    Error { reason: ErrorReason },
}

/// Error reasons returned by the server.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorReason {
    /// The server has restarted and all sessions are invalidated.
    ServerRestarted,
    /// Invalid or missing connection nonce.
    InvalidNonce,
    /// Mojang verification failed.
    VerificationFailed,
    /// The client is not registered.
    NotRegistered,
    /// The client is not discoverable.
    NotDiscoverable,
    /// Invalid message format.
    InvalidMessage,
    /// The target peer was not found, not discoverable, or request failed.
    /// Intentionally vague for privacy.
    PeerUnavailable,
    /// No pending request from this peer.
    NoPendingRequest,
    /// Internal server error.
    InternalError,
}

impl std::fmt::Display for ErrorReason {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ErrorReason::ServerRestarted => write!(f, "server_restarted"),
            ErrorReason::InvalidNonce => write!(f, "invalid_nonce"),
            ErrorReason::VerificationFailed => write!(f, "verification_failed"),
            ErrorReason::NotRegistered => write!(f, "not_registered"),
            ErrorReason::NotDiscoverable => write!(f, "not_discoverable"),
            ErrorReason::InvalidMessage => write!(f, "invalid_message"),
            ErrorReason::PeerUnavailable => write!(f, "peer_unavailable"),
            ErrorReason::NoPendingRequest => write!(f, "no_pending_request"),
            ErrorReason::InternalError => write!(f, "internal_error"),
        }
    }
}
