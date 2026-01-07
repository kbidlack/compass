//! Mojang API client for Minecraft session verification.
//!
//! This module handles the server-side verification of Minecraft player
//! identities using Mojang's session server API.

#![allow(dead_code)]

use reqwest::Client;
use serde::Deserialize;
use thiserror::Error;

/// Base URL for the Mojang session server.
const SESSION_SERVER_URL: &str = "https://sessionserver.mojang.com";

/// Errors that can occur during Mojang API operations.
#[derive(Debug, Error)]
pub enum MojangError {
    #[error("HTTP request failed: {0}")]
    Request(#[from] reqwest::Error),

    #[error("Verification failed: player has not joined")]
    NotJoined,

    #[error("Username mismatch: expected {expected}, got {actual}")]
    UsernameMismatch { expected: String, actual: String },

    #[error("UUID mismatch: expected {expected}, got {actual}")]
    UuidMismatch { expected: String, actual: String },
}

/// Response from the Mojang hasJoined endpoint.
#[derive(Debug, Deserialize)]
pub struct HasJoinedResponse {
    /// The player's UUID (without dashes).
    pub id: String,
    /// The player's username (case-sensitive).
    pub name: String,
}

/// Client for interacting with the Mojang session server API.
#[derive(Debug, Clone)]
pub struct MojangClient {
    client: Client,
}

impl Default for MojangClient {
    fn default() -> Self {
        Self::new()
    }
}

impl MojangClient {
    /// Create a new Mojang API client.
    pub fn new() -> Self {
        Self {
            client: Client::new(),
        }
    }

    /// Verify that a player has joined the session with the given server ID.
    ///
    /// This implements the server-side of Minecraft's authentication protocol:
    /// 1. Client calls POST /session/minecraft/join with their access token
    /// 2. Server calls GET /session/minecraft/hasJoined to verify
    ///
    /// # Arguments
    /// * `username` - The player's username (case-insensitive for the query)
    /// * `server_id` - The server ID that was sent to the client as a challenge
    ///
    /// # Returns
    /// The verified player information on success.
    pub async fn verify_session(
        &self,
        username: &str,
        server_id: &str,
    ) -> Result<HasJoinedResponse, MojangError> {
        let url = format!(
            "{}/session/minecraft/hasJoined?username={}&serverId={}",
            SESSION_SERVER_URL, username, server_id
        );

        let response = self.client.get(&url).send().await?;

        // Mojang returns 204 No Content if the player hasn't joined
        if response.status() == reqwest::StatusCode::NO_CONTENT {
            return Err(MojangError::NotJoined);
        }

        // Ensure we got a success status
        let response = response.error_for_status()?;

        // Parse the response
        let player_info: HasJoinedResponse = response.json().await?;

        Ok(player_info)
    }

    /// Verify that a player has joined and validate their identity.
    ///
    /// This combines the hasJoined check with identity validation.
    ///
    /// # Arguments
    /// * `username` - The expected username
    /// * `uuid` - The expected UUID (with or without dashes)
    /// * `server_id` - The server ID challenge
    ///
    /// # Returns
    /// The verified player information on success.
    pub async fn verify_player(
        &self,
        username: &str,
        uuid: &str,
        server_id: &str,
    ) -> Result<HasJoinedResponse, MojangError> {
        let response = self.verify_session(username, server_id).await?;

        // Normalize UUID for comparison (remove dashes)
        let expected_uuid = uuid.replace('-', "").to_lowercase();
        let actual_uuid = response.id.to_lowercase();

        if expected_uuid != actual_uuid {
            return Err(MojangError::UuidMismatch {
                expected: expected_uuid,
                actual: actual_uuid,
            });
        }

        Ok(response)
    }
}

/// Generate a random server ID for Mojang verification.
///
/// In real Minecraft, this is computed from cryptographic handshake data.
/// For our rendezvous service, we generate a random hex string.
pub fn generate_server_id() -> String {
    use rand::RngCore;

    let mut bytes = [0u8; 16];
    rand::thread_rng().fill_bytes(&mut bytes);

    // Format as a hex string (similar to Minecraft's server ID format)
    base16ct::lower::encode_string(&bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_generate_server_id() {
        let id1 = generate_server_id();
        let id2 = generate_server_id();

        // Should be 32 hex characters
        assert_eq!(id1.len(), 32);
        assert_eq!(id2.len(), 32);

        // Should be different
        assert_ne!(id1, id2);

        // Should be valid hex
        assert!(id1.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn test_uuid_normalization() {
        // UUIDs with and without dashes should match
        let uuid_with_dashes = "853c80ef-3c37-49fd-aa49-938b674adae6";
        let uuid_without = "853c80ef3c3749fdaa49938b674adae6";

        let normalized_with = uuid_with_dashes.replace('-', "").to_lowercase();
        let normalized_without = uuid_without.to_lowercase();

        assert_eq!(normalized_with, normalized_without);
    }
}
