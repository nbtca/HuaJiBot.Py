#!/usr/bin/env bash
set -eu
libraries=$(realpath "${1:?pass the Forge server libraries directory}")
cd "$(dirname "$0")"
classes=$(mktemp -d)
trap 'rm -rf "$classes"' EXIT
classpath=$(find "$libraries" -name '*.jar' -printf '%p:')
javac --release 17 -proc:none -cp "$classpath" -d "$classes" src/PlayerStatus.java
jar --create --file huaji-player-status-1.0.0.jar -C "$classes" . -C resources .
