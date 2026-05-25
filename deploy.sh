#!/usr/bin/env bash
# Pull the latest version of hertford from GitHub and restart the stack.
#
# Run on the Synology:
#     cd /volume1/docker/hertford && ./deploy.sh
#
# Or remotely from anywhere with SSH access:
#     ssh msdeegan@192.168.8.237 'cd /volume1/docker/hertford && ./deploy.sh'
#
# Prereq for non-interactive ssh: passwordless sudo for the docker binary.
# On the Synology, once:
#   echo "$USER ALL=(root) NOPASSWD: /usr/local/bin/docker" | sudo tee /etc/sudoers.d/hertford
#   sudo chmod 0440 /etc/sudoers.d/hertford
#
# Environment overrides:
#   HERTFORD_BRANCH   git branch to pull from (default: dev)
#   HERTFORD_REPO_URL tarball URL (default: github.com/msdeegan/hertford)

set -euo pipefail

BRANCH="${HERTFORD_BRANCH:-dev}"
REPO_URL="${HERTFORD_REPO_URL:-https://github.com/msdeegan/hertford}"
TARBALL_URL="${REPO_URL}/archive/refs/heads/${BRANCH}.tar.gz"
# Full path to docker — the NOPASSWD sudoers entry targets this exact path,
# and sudo's own PATH may otherwise resolve `docker` to a different binary
# (Container Manager ships its own copy alongside /usr/local/bin/docker).
DOCKER="${HERTFORD_DOCKER:-/usr/local/bin/docker}"

cd "$(dirname "$(readlink -f "$0")")"

echo "→ pulling ${BRANCH} from ${REPO_URL}"
# --strip-components=1 drops the tarball's top-level hertford-<branch>/ dir.
# .env lives in infra/ and is gitignored, so it isn't in the tarball — safe.
curl -fsSL "${TARBALL_URL}" | tar xz --strip-components=1

if [ ! -f infra/.env ]; then
    echo "✗ infra/.env is missing — copy infra/.env.example, fill it in, then re-run."
    exit 1
fi

echo "→ rebuilding and restarting"
cd infra
sudo "$DOCKER" compose up -d --build

echo "→ status"
sudo "$DOCKER" compose ps

echo "→ recent hertford logs"
sudo "$DOCKER" compose logs --tail=10 hertford
