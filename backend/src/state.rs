//! Server state management.
//!
//! All state is ephemeral and held in memory. On server restart,
//! all sessions, challenges, and connection requests are invalidated.

#![allow(dead_code)]

use std::time::{Duration, Instant};

use dashmap::DashMap;
use iroh::PublicKey;
use tokio::sync::{mpsc, oneshot};

/// Duration after which pending challenges expire.
const CHALLENGE_TIMEOUT: Duration = Duration::from_secs(60);

/// Duration after which sessions expire without heartbeat.
const SESSION_TIMEOUT: Duration = Duration::from_secs(300);

/// Duration after which pending connection requests expire.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(60);

/// Notification sent to a session about an incoming connection request.
/// Contains only uuid/username - node_id is NOT shared until accepted.
#[derive(Debug, Clone)]
pub struct ConnectionRequestNotification {
    pub from_uuid: String,
    pub from_username: String,
    pub reason: Option<String>,
}

/// Notification sent when a connection is accepted (contains full peer info).
#[derive(Debug, Clone)]
pub struct PeerNotification {
    pub mc_uuid: String,
    pub mc_username: String,
    pub node_id: PublicKey,
}

/// Messages that can be sent to a session's notification channel.
#[derive(Debug)]
pub enum SessionNotification {
    /// Incoming connection request (uuid/username only).
    ConnectionRequest(ConnectionRequestNotification),
    /// Connection accepted - full peer info.
    Peer(PeerNotification),
}

/// An active session for a verified Minecraft player.
#[derive(Debug)]
pub struct Session {
    /// The iroh node ID of the connected client.
    pub node_id: PublicKey,
    /// Minecraft UUID.
    pub mc_uuid: String,
    /// Minecraft username.
    pub mc_username: String,
    /// When the session was registered.
    pub registered_at: Instant,
    /// Last activity timestamp.
    pub last_seen: Instant,
    /// Whether this client is discoverable by others.
    pub discoverable: bool,
    /// Channel to send notifications to this session.
    pub notify_tx: mpsc::Sender<SessionNotification>,
}

impl Clone for Session {
    fn clone(&self) -> Self {
        Self {
            node_id: self.node_id,
            mc_uuid: self.mc_uuid.clone(),
            mc_username: self.mc_username.clone(),
            registered_at: self.registered_at,
            last_seen: self.last_seen,
            discoverable: self.discoverable,
            notify_tx: self.notify_tx.clone(),
        }
    }
}

/// A pending challenge awaiting Mojang verification.
#[derive(Debug)]
pub struct PendingChallenge {
    /// The iroh node ID of the client.
    pub node_id: PublicKey,
    /// Minecraft UUID claimed by the client.
    pub mc_uuid: String,
    /// Minecraft username claimed by the client.
    pub mc_username: String,
    /// The server ID for Mojang verification.
    pub server_id: String,
    /// When the challenge was created.
    pub created_at: Instant,
    /// Channel to send notifications (passed to session on verification).
    pub notify_tx: mpsc::Sender<SessionNotification>,
}

impl Clone for PendingChallenge {
    fn clone(&self) -> Self {
        Self {
            node_id: self.node_id,
            mc_uuid: self.mc_uuid.clone(),
            mc_username: self.mc_username.clone(),
            server_id: self.server_id.clone(),
            created_at: self.created_at,
            notify_tx: self.notify_tx.clone(),
        }
    }
}

/// Result of a connection request (sent back to the requester).
#[derive(Debug)]
pub enum ConnectionResult {
    /// Connection accepted - contains the target's full info.
    Accepted(PeerNotification),
    /// Connection declined or failed.
    Declined,
}

/// A pending connection request from one peer to another.
#[derive(Debug)]
pub struct PendingRequest {
    /// The UUID of the requester.
    pub from_uuid: String,
    /// The username of the requester.
    pub from_username: String,
    /// The node ID of the requester.
    pub from_node_id: PublicKey,
    /// The UUID of the target.
    pub to_uuid: String,
    /// Optional reason for the request.
    pub reason: Option<String>,
    /// When the request was created.
    pub created_at: Instant,
    /// Channel to send the result back to the requester.
    pub result_tx: oneshot::Sender<ConnectionResult>,
}

/// Key for pending requests: (from_uuid, to_uuid)
#[derive(Debug, Clone, Hash, PartialEq, Eq)]
pub struct RequestKey {
    pub from_uuid: String,
    pub to_uuid: String,
}

impl RequestKey {
    pub fn new(from_uuid: &str, to_uuid: &str) -> Self {
        Self {
            from_uuid: from_uuid.to_string(),
            to_uuid: to_uuid.to_string(),
        }
    }
}

/// The server's ephemeral state.
#[derive(Debug)]
pub struct State {
    /// Active sessions indexed by Minecraft UUID.
    sessions_by_uuid: DashMap<String, Session>,

    /// Pending challenges indexed by node_id.
    challenges: DashMap<PublicKey, PendingChallenge>,

    /// Pending connection requests indexed by (from_uuid, to_uuid).
    pending_requests: DashMap<RequestKey, PendingRequest>,
}

impl Default for State {
    fn default() -> Self {
        Self::new()
    }
}

impl State {
    /// Create a new empty state.
    pub fn new() -> Self {
        Self {
            sessions_by_uuid: DashMap::new(),
            challenges: DashMap::new(),
            pending_requests: DashMap::new(),
        }
    }

    /// Check if a session exists for the given Minecraft UUID.
    pub fn has_session(&self, mc_uuid: &str) -> bool {
        self.sessions_by_uuid.contains_key(mc_uuid)
    }

    /// Get a session by Minecraft UUID.
    pub fn get_session(&self, mc_uuid: &str) -> Option<Session> {
        self.sessions_by_uuid.get(mc_uuid).map(|s| s.clone())
    }

    /// Get a session by node ID.
    pub fn get_session_by_node(&self, node_id: &PublicKey) -> Option<Session> {
        self.sessions_by_uuid
            .iter()
            .find(|entry| entry.value().node_id == *node_id)
            .map(|entry| entry.value().clone())
    }

    /// Insert or replace a session.
    /// If a session already exists for this UUID, it is replaced.
    pub fn upsert_session(&self, session: Session) {
        let mc_uuid = session.mc_uuid.clone();
        // Remove any pending requests for the old session
        self.pending_requests
            .retain(|key, _| key.from_uuid != mc_uuid && key.to_uuid != mc_uuid);
        // Insert/replace the session
        self.sessions_by_uuid.insert(mc_uuid, session);
    }

    /// Update the last_seen timestamp for a session.
    pub fn touch_session(&self, mc_uuid: &str) {
        if let Some(mut session) = self.sessions_by_uuid.get_mut(mc_uuid) {
            session.last_seen = Instant::now();
        }
    }

    /// Set a session as discoverable.
    pub fn set_discoverable(&self, mc_uuid: &str, discoverable: bool) {
        if let Some(mut session) = self.sessions_by_uuid.get_mut(mc_uuid) {
            session.discoverable = discoverable;
        }
    }

    /// Remove a session by Minecraft UUID.
    pub fn remove_session(&self, mc_uuid: &str) {
        self.sessions_by_uuid.remove(mc_uuid);
        // Also remove any pending requests involving this session
        self.pending_requests
            .retain(|key, _| key.from_uuid != mc_uuid && key.to_uuid != mc_uuid);
    }

    /// Remove a session by node ID.
    pub fn remove_session_by_node(&self, node_id: &PublicKey) {
        // Find the UUID first
        let uuid = self
            .sessions_by_uuid
            .iter()
            .find(|entry| entry.value().node_id == *node_id)
            .map(|entry| entry.key().clone());

        if let Some(uuid) = uuid {
            self.remove_session(&uuid);
        }
    }

    /// Store a pending challenge.
    pub fn insert_challenge(&self, challenge: PendingChallenge) {
        self.challenges.insert(challenge.node_id, challenge);
    }

    /// Get and remove a pending challenge by node ID.
    pub fn take_challenge(&self, node_id: &PublicKey) -> Option<PendingChallenge> {
        self.challenges.remove(node_id).map(|(_, c)| c)
    }

    /// Insert a pending connection request.
    /// Returns the receiver for the result if successful, or None if a request already exists.
    pub fn insert_pending_request(&self, request: PendingRequest) -> bool {
        let key = RequestKey::new(&request.from_uuid, &request.to_uuid);
        if self.pending_requests.contains_key(&key) {
            return false;
        }
        self.pending_requests.insert(key, request);
        true
    }

    /// Get a pending request by key.
    pub fn get_pending_request(&self, from_uuid: &str, to_uuid: &str) -> Option<PendingRequest> {
        let key = RequestKey::new(from_uuid, to_uuid);
        self.pending_requests.remove(&key).map(|(_, r)| r)
    }

    /// Take (remove and return) a pending request.
    pub fn take_pending_request(&self, from_uuid: &str, to_uuid: &str) -> Option<PendingRequest> {
        let key = RequestKey::new(from_uuid, to_uuid);
        self.pending_requests.remove(&key).map(|(_, r)| r)
    }

    /// Check if there's a pending request from one peer to another.
    pub fn has_pending_request(&self, from_uuid: &str, to_uuid: &str) -> bool {
        let key = RequestKey::new(from_uuid, to_uuid);
        self.pending_requests.contains_key(&key)
    }

    /// Clean up expired entries.
    pub fn cleanup_expired(&self) {
        let now = Instant::now();

        // Remove expired sessions
        self.sessions_by_uuid
            .retain(|_, session| now.duration_since(session.last_seen) < SESSION_TIMEOUT);

        // Remove expired challenges
        self.challenges
            .retain(|_, challenge| now.duration_since(challenge.created_at) < CHALLENGE_TIMEOUT);

        // Remove expired pending requests (and notify requesters of failure)
        self.pending_requests.retain(|_, request| {
            if now.duration_since(request.created_at) >= REQUEST_TIMEOUT {
                // Send declined result to the requester (best effort)
                let _ = std::mem::replace(&mut request.result_tx, oneshot::channel().0);
                false
            } else {
                true
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_node_id() -> PublicKey {
        let secret = iroh::SecretKey::generate(&mut rand::thread_rng());
        secret.public()
    }

    fn create_test_session(
        node_id: PublicKey,
        mc_uuid: &str,
    ) -> (Session, mpsc::Receiver<SessionNotification>) {
        let (tx, rx) = mpsc::channel(16);
        let session = Session {
            node_id,
            mc_uuid: mc_uuid.to_string(),
            mc_username: format!("Player_{}", mc_uuid),
            registered_at: Instant::now(),
            last_seen: Instant::now(),
            discoverable: false,
            notify_tx: tx,
        };
        (session, rx)
    }

    #[test]
    fn test_session_lifecycle() {
        let state = State::new();
        let node_id = test_node_id();

        let (session, _rx) = create_test_session(node_id, "test-uuid");

        // Insert first session
        state.upsert_session(session.clone());
        assert!(state.has_session("test-uuid"));
        assert!(state.get_session("test-uuid").is_some());

        // Create a new session with same UUID - should replace the old one
        let node_id2 = test_node_id();
        let (session2, _rx2) = create_test_session(node_id2, "test-uuid");
        state.upsert_session(session2);

        // Session should still exist but with new node_id
        assert!(state.has_session("test-uuid"));
        let retrieved = state.get_session("test-uuid").unwrap();
        assert_eq!(retrieved.node_id, node_id2);

        state.remove_session("test-uuid");
        assert!(!state.has_session("test-uuid"));
    }

    #[test]
    fn test_pending_request() {
        let state = State::new();
        let node_a = test_node_id();

        let (result_tx, _result_rx) = oneshot::channel();

        let request = PendingRequest {
            from_uuid: "uuid-a".to_string(),
            from_username: "PlayerA".to_string(),
            from_node_id: node_a,
            to_uuid: "uuid-b".to_string(),
            reason: Some("Let's play!".to_string()),
            created_at: Instant::now(),
            result_tx,
        };

        // Insert should succeed
        assert!(state.insert_pending_request(request));

        // Check it exists
        assert!(state.has_pending_request("uuid-a", "uuid-b"));
        assert!(!state.has_pending_request("uuid-b", "uuid-a")); // Different direction

        // Take it
        let taken = state.take_pending_request("uuid-a", "uuid-b");
        assert!(taken.is_some());
        assert_eq!(taken.unwrap().from_uuid, "uuid-a");

        // Should be gone now
        assert!(!state.has_pending_request("uuid-a", "uuid-b"));
    }
}
