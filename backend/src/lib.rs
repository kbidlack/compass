//! Compass - Rendezvous Service Library
//!
//! A privacy-preserving peer discovery service for Minecraft players
//! using iroh P2P networking.
//!
//! This library provides the core components for running a rendezvous
//! server that enables Minecraft players to discover each other without
//! exposing their IP addresses until mutual consent is established.
//!
//! ## Design Principles
//!
//! * **Zero trust in server** — Server cannot impersonate clients or read P2P traffic
//! * **Zero persistent state** — All data expires; nothing written to disk
//! * **Zero IP exposure** — IPs never stored; peers only learn IPs after mutual consent
//! * **Minimal protocol** — iroh handles auth; server just verifies Minecraft ownership
//!
//! ## Example
//!
//! ```no_run
//! use std::sync::Arc;
//! use compass::{RendezvousHandler, State, ALPN};
//! use iroh::protocol::Router;
//! use iroh::Endpoint;
//!
//! # async fn example() -> Result<(), Box<dyn std::error::Error>> {
//! // Create the endpoint
//! let endpoint = Endpoint::builder()
//!     .alpns(vec![ALPN.to_vec()])
//!     .bind()
//!     .await?;
//!
//! // Create shared state
//! let state = Arc::new(State::new());
//!
//! // Create the protocol handler
//! let handler = RendezvousHandler::new(state);
//!
//! // Build and spawn the router
//! let router = Router::builder(endpoint.clone())
//!     .accept(ALPN, handler)
//!     .spawn();
//! # Ok(())
//! # }
//! ```

pub mod handler;
pub mod mojang;
pub mod protocol;
pub mod state;

// Re-export main types for convenience
pub use handler::RendezvousHandler;
pub use mojang::{generate_server_id, MojangClient, MojangError};
pub use protocol::{ClientMessage, ErrorReason, ServerMessage, ALPN};
pub use state::{PendingChallenge, Session, State};
