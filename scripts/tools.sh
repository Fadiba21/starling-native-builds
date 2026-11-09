#!/usr/bin/env bash
# What the clean cache holds besides the build: the toolchain, the Android tools build_apk.py pins, the
# crates, and horizon-mixins, none of which the sandbox can download. Only for base.yml.
set -euo pipefail

root=$HOME/modbuild

rm -rf "$HOME/.rustup" "$HOME/.cargo"
curl -fsSL https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal \
  --default-toolchain stable --target aarch64-linux-android
# shellcheck disable=SC1091
. "$HOME/.cargo/env"
cargo install cargo-ndk --version 3.5.4 --locked

# `yes` ends on a broken pipe when the licenses are all taken, which pipefail would count as a failure.
(yes || true) | "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$HOME/android" \
  --install "build-tools;35.0.0" "platforms;android-36" "ndk;28.2.13676358" | tail -3
ls "$HOME/android/ndk/28.2.13676358/toolchains" "$HOME/android/build-tools/35.0.0/aapt2" \
  "$HOME/android/platforms/android-36/android.jar"

rev=$(sed -n 's/^MIXINS_REV=//p' "$root/source/packaging/android/native_build.sh")
git_url=$(sed -n 's/^MIXINS_GIT=//p' "$root/source/packaging/android/native_build.sh")
cargo install --locked --git "$git_url" --rev "$rev" --root "$root/base/mixins-${rev:0:12}" horizon-mixins-cli

cargo fetch --locked --manifest-path "$root/source/Cargo.toml"

# The sandbox's home has this folder as its .cargo.
rm -rf "$root/cargo"
mv "$HOME/.cargo" "$root/cargo"
