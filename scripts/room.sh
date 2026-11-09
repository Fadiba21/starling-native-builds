#!/usr/bin/env bash
# What the runner comes with and these builds don't use, and swap: the game's fast profile can want more
# than the runner's memory. Disk space is what runs out first without this.
set -euo pipefail

sudo rm -rf /usr/local/lib/android /usr/share/dotnet /opt/ghc /usr/local/.ghcup \
  /opt/hostedtoolcache/CodeQL /usr/local/share/boost /usr/local/share/powershell /usr/share/swift

where=/mnt
avail=$(df --output=avail -BG "$where" 2>/dev/null | tail -1 | tr -dc 0-9)
[ "${avail:-0}" -ge 24 ] || where=$RUNNER_TEMP
sudo swapoff -a || true
sudo fallocate -l 16G "$where/build.swap"
sudo chmod 600 "$where/build.swap"
sudo mkswap "$where/build.swap" > /dev/null
sudo swapon "$where/build.swap"
df -h / "$where"
free -g
