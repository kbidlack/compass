//! Compass - Rendezvous Service
//!
//! A privacy-preserving peer discovery service for Minecraft players
//! using iroh P2P networking.

mod handler;
mod mojang;
mod protocol;
mod state;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use iroh::protocol::Router;
use iroh::{Endpoint, SecretKey};

use tracing::{info, warn};

use crate::handler::RendezvousHandler;
use crate::protocol::ALPN;
use crate::state::State;

/// Command-line arguments for the rendezvous server.
#[derive(Debug, Parser)]
#[command(name = "compass")]
#[command(about = "Privacy-preserving peer discovery service for Minecraft players")]
struct Args {
    /// Path to the secret key file. If not specified, a new key is generated.
    #[arg(short, long)]
    key_file: Option<PathBuf>,

    /// Interval in seconds for cleaning up expired sessions.
    #[arg(long, default_value = "60")]
    cleanup_interval: u64,
}

/// Load or generate the server's secret key.
fn load_or_generate_key(path: Option<&PathBuf>) -> Result<SecretKey, Box<dyn std::error::Error>> {
    match path {
        Some(path) if path.exists() => {
            let key_bytes = std::fs::read(path)?;
            let key_array: [u8; 32] = key_bytes
                .try_into()
                .map_err(|_| "Invalid key file: expected 32 bytes")?;
            let key = SecretKey::from_bytes(&key_array);
            info!(?path, "Loaded secret key from file");
            Ok(key)
        }
        Some(path) => {
            let key = SecretKey::generate(&mut rand::thread_rng());
            let key_bytes = key.to_bytes();
            std::fs::write(path, key_bytes)?;
            info!(?path, "Generated and saved new secret key");
            Ok(key)
        }
        None => {
            let key = SecretKey::generate(&mut rand::thread_rng());
            warn!("Generated ephemeral secret key (not persisted)");
            Ok(key)
        }
    }
}

/// Spawn a background task to periodically clean up expired state.
fn spawn_cleanup_task(state: Arc<State>, interval: Duration) {
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(interval);
        loop {
            ticker.tick().await;
            state.cleanup_expired();
        }
    });
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize logging
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive("compass=info".parse()?)
                .add_directive("iroh=warn".parse()?),
        )
        .init();

    let args = Args::parse();

    // Load or generate secret key
    let secret_key = load_or_generate_key(args.key_file.as_ref())?;
    let public_key = secret_key.public();

    info!("Server NodeId: {}", public_key);

    // Create the endpoint with n0 DNS discovery enabled
    // This publishes our address to the n0 DNS service so clients can find us by NodeId
    // Note: Don't set .alpns() here - let the Router handle ALPN registration
    let endpoint = Endpoint::builder()
        .secret_key(secret_key)
        .discovery_n0()
        .bind()
        .await?;

    // Log connection information
    let node_id = endpoint.node_id();
    info!("Node ID: {}", node_id);

    // Wait for relay connection to establish and log the relay URL
    let mut relay_watcher = endpoint.home_relay();
    match tokio::time::timeout(Duration::from_secs(10), relay_watcher.initialized()).await {
        Ok(Ok(relay_url)) => {
            info!("Relay URL: {}", relay_url);
        }
        Ok(Err(_)) => {
            warn!("Relay watcher disconnected - clients will need direct connectivity");
        }
        Err(_) => {
            warn!("Timeout waiting for relay connection - clients will need direct connectivity");
        }
    }

    // Create shared state
    let state = Arc::new(State::new());

    // Start cleanup task
    spawn_cleanup_task(
        Arc::clone(&state),
        Duration::from_secs(args.cleanup_interval),
    );

    // Create the protocol handler
    let handler = RendezvousHandler::new(state);

    // Build and spawn the router
    let router = Router::builder(endpoint.clone())
        .accept(ALPN, handler)
        .spawn();

    info!("Rendezvous server is running");
    info!("Press Ctrl+C to stop");

    // Wait for shutdown signal
    tokio::signal::ctrl_c().await?;

    info!("Shutting down...");

    // Gracefully shut down
    router.shutdown().await?;
    endpoint.close().await;

    info!("Server stopped");

    Ok(())
}
