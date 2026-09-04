#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ELAN_HOME="$PROJECT_DIR/.elan"
export ELAN_HOME

if [ ! -x "$ELAN_HOME/bin/elan" ]; then
  curl -sSf https://elan.lean-lang.org/elan-init.sh | sh -s -- -y --no-modify-path --default-toolchain none
fi

"$ELAN_HOME/bin/elan" toolchain install leanprover/lean4:v4.33.0
"$ELAN_HOME/bin/lean" --version

