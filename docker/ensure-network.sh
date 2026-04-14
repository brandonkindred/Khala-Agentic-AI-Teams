#!/usr/bin/env bash
# Create the khala-stack network if it doesn't exist (required for compose when using external network).
# Run once before first 'podman compose up' or after 'podman network rm khala-stack'.
# Prevents repeated "IPAM option driver has changed" errors with Podman + docker-compose.

set -e

NETWORK_NAME="${1:-khala-stack}"
SUBNET="${2:-172.28.0.0/24}"

if command -v podman &>/dev/null; then
  if podman network exists "$NETWORK_NAME" 2>/dev/null; then
    echo "Network $NETWORK_NAME already exists."
    exit 0
  fi
  echo "Creating Podman network $NETWORK_NAME with subnet $SUBNET ..."
  podman network create --subnet "$SUBNET" "$NETWORK_NAME"
elif command -v docker &>/dev/null; then
  if docker network inspect "$NETWORK_NAME" &>/dev/null; then
    echo "Network $NETWORK_NAME already exists."
    exit 0
  fi
  echo "Creating Docker network $NETWORK_NAME with subnet $SUBNET ..."
  docker network create --subnet "$SUBNET" "$NETWORK_NAME"
else
  echo "Neither podman nor docker found. Install one of them." >&2
  exit 1
fi

echo "Done. You can run: podman compose -f docker/docker-compose.yml --env-file docker/.env up -d"
