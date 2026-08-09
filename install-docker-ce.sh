#!/usr/bin/env bash
set -e

echo "=== 1. Removing Fedora built-in Moby / Docker packages ==="
sudo dnf remove -y moby-engine moby-filesystem docker-cli docker-compose docker-compose-switch docker-buildx moby-engine-rootless-extras moby-engine-nano || true

echo "=== 2. Adding official Docker CE repository ==="
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo

echo "=== 3. Installing official Docker Engine & Docker Compose ==="
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo "=== 4. Starting and enabling Docker service ==="
sudo systemctl enable --now docker

echo "=== 5. Adding $USER to docker group ==="
sudo usermod -aG docker "$USER"

echo ""
echo "=== Complete! Run 'newgrp docker' or log out and back in to apply group permissions without sudo. ==="
