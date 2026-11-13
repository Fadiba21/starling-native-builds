#!/usr/bin/env bash
# What every job needs first: the version's commit, its source as the sandbox expects it, and the build
# scripts. The sandbox's paths are the build cache's, so they never change: ~/modbuild, ~/android, ~/.rustup.
set -euo pipefail

COMMIT=$(python3 - <<'EOF'
import json, os, sys
versions = json.load(open("versions.json"))
version = os.environ["VERSION"]
if version not in versions:
    sys.exit(f"No commit for version {version!r} in versions.json")
print(versions[version])
EOF
)
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "Not a commit: $COMMIT"; exit 1; }
echo "COMMIT=$COMMIT" >> "$GITHUB_ENV"

mkdir -p tools
for name in mod_build_sandbox.sh mod_build_server.py mod_build_once.py; do
  curl -fsS --retry 5 -o "tools/$name" \
    "https://raw.githubusercontent.com/starling-client/starling/${TOOLS:-starling}/packaging/android/$name"
done

root=$HOME/modbuild
mkdir -p "$root/jobs" "$root/base"
git init -q "$root/source"
git -C "$root/source" fetch -q --depth 1 https://github.com/starling-client/starling "$COMMIT"
git -C "$root/source" checkout -q --detach FETCH_HEAD
python3 scripts/patch_native_mod_adapter.py --install-sandbox-hook tools/mod_build_sandbox.sh

# What the build reads when it starts, in the sandbox's home.
cat > "$HOME/.profile" <<'EOF'
export ANDROID_HOME=$HOME/android
EOF
