#!/usr/bin/env bash
set -euo pipefail

# Route GitHub HTTPS git auth through gh, whichever way gh is authenticated
# (GH_TOKEN from .env or Settings, or a browser login stored by gh).
git config --global --replace-all credential.https://github.com.helper ''
git config --global --add credential.https://github.com.helper '!gh auth git-credential'

lazyreview config --init >/dev/null

exec "$@"
