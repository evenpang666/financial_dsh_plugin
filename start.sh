#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export FINANCE_ROOT="$PWD"
. "$FINANCE_ROOT/scripts/bootstrap-node.sh"
financial_prepare_node
exec "$FINANCE_NODE" scripts/launch.mjs "$@"
