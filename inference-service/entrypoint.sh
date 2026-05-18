#!/bin/sh
set -e
# Fix HuggingFace cache volume permissions at container start.
# Docker named volumes are created owned by root; the server process runs as
# scalestyle (uid 1000) so it can't write model files without this chown.
chown -R scalestyle:scalestyle /cache/huggingface 2>/dev/null || true
exec gosu scalestyle "$@"
