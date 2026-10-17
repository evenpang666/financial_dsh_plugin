#!/usr/bin/env sh
# Sourced by start.sh; keep the runtime and PATH local to this launch.
financial_node_ok() {
  "$1" -e 'const fs=require("fs"),m=JSON.parse(fs.readFileSync(process.argv[1])).nodeMinimum.split(".").map(Number),v=process.versions.node.split(".").map(Number);for(let i=0;i<3;i++){if(v[i]>m[i])break;if(v[i]<m[i])process.exit(1)}' "$FINANCE_ROOT/scripts/runtime-versions.json" >/dev/null 2>&1
}

financial_download() {
  if command -v curl >/dev/null 2>&1; then
    curl --fail --location --connect-timeout 10 --max-time 120 --retry 1 --output "$2" "$1" ||
      curl --noproxy '*' --fail --location --connect-timeout 10 --max-time 120 --output "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget --timeout=30 --tries=2 -O "$2" "$1" ||
      HTTP_PROXY= HTTPS_PROXY= ALL_PROXY= http_proxy= https_proxy= all_proxy= wget --no-proxy --timeout=30 --tries=1 -O "$2" "$1"
  else
    printf '%s\n' 'Automatic Node.js setup needs curl or wget. Install one and start again.' >&2
    return 1
  fi
}

financial_install_node() (
  set -eu
  case "$(uname -s)" in Linux) financial_os=linux ;; Darwin) financial_os=darwin ;; *) printf '%s\n' 'Unsupported system. Use start.bat on Windows.' >&2; exit 1 ;; esac
  case "$(uname -m)" in x86_64|amd64) financial_arch=x64 ;; aarch64|arm64) financial_arch=arm64 ;; *) printf '%s\n' 'Automatic Node.js setup supports x64 and arm64.' >&2; exit 1 ;; esac
  command -v tar >/dev/null 2>&1 || { printf '%s\n' 'Automatic Node.js setup needs tar.' >&2; exit 1; }
  mkdir -p "$FINANCE_ROOT/.runtime/node" || exit 1
  financial_stage=$(mktemp -d "$FINANCE_ROOT/.runtime/node/.install-XXXXXX") || exit 1
  trap 'rm -rf "$financial_stage"' EXIT
  trap 'exit 130' INT TERM
  financial_release=$(sed -n 's/.*"node": "\([0-9.]*\)".*/\1/p' "$FINANCE_ROOT/scripts/runtime-versions.json")
  [ -n "$financial_release" ] || exit 1
  printf '%s\n' "[环境] 补齐项目内 Node.js $financial_release 和 npm…"
  financial_download "https://nodejs.org/dist/v$financial_release/SHASUMS256.txt" "$financial_stage/SHASUMS256.txt" || exit 1
  financial_archive="node-v$financial_release-$financial_os-$financial_arch.tar.gz"
  [ -n "$financial_archive" ] || { printf '%s\n' 'The official Node.js archive was not found for this platform.' >&2; exit 1; }
  financial_release=${financial_archive#node-}; financial_release=${financial_release%%-*}
  financial_download "https://nodejs.org/dist/$financial_release/$financial_archive" "$financial_stage/$financial_archive" || exit 1
  financial_expected=$(awk -v name="$financial_archive" '$2==name {print $1}' "$financial_stage/SHASUMS256.txt")
  [ -n "$financial_expected" ] || { printf '%s\n' 'Official archive checksum not found.' >&2; exit 1; }
  if command -v sha256sum >/dev/null 2>&1; then financial_actual=$(sha256sum "$financial_stage/$financial_archive" | awk '{print $1}')
  elif command -v shasum >/dev/null 2>&1; then financial_actual=$(shasum -a 256 "$financial_stage/$financial_archive" | awk '{print $1}')
  elif command -v openssl >/dev/null 2>&1; then financial_actual=$(openssl dgst -sha256 "$financial_stage/$financial_archive" | awk '{print $NF}')
  else printf '%s\n' 'No SHA-256 verification tool is available.' >&2; exit 1; fi
  [ "$financial_actual" = "$financial_expected" ] || { printf '%s\n' 'Node.js download checksum mismatch. Start again to retry.' >&2; exit 1; }
  tar -xzf "$financial_stage/$financial_archive" -C "$financial_stage" || exit 1
  financial_unpacked="$financial_stage/${financial_archive%.tar.gz}"
  financial_node_ok "$financial_unpacked/bin/node" || { printf '%s\n' 'Downloaded Node.js cannot run on this system.' >&2; exit 1; }
  "$financial_unpacked/bin/node" "$financial_unpacked/lib/node_modules/npm/bin/npm-cli.js" --version >/dev/null || exit 1
  if [ -e "$FINANCE_ROOT/.runtime/node/current" ]; then mv "$FINANCE_ROOT/.runtime/node/current" "$financial_stage/previous"; fi
  if ! mv "$financial_unpacked" "$FINANCE_ROOT/.runtime/node/current"; then
    if [ -e "$financial_stage/previous" ]; then mv "$financial_stage/previous" "$FINANCE_ROOT/.runtime/node/current"; fi
    exit 1
  fi
)

financial_prepare_node() {
  FINANCE_NODE=$(command -v node || true)
  financial_system_node=$FINANCE_NODE
  if [ -n "$FINANCE_NODE" ] && financial_node_ok "$FINANCE_NODE" && command -v npm >/dev/null 2>&1 && npm --version >/dev/null 2>&1; then
    PATH="$(dirname -- "$FINANCE_NODE"):$PATH"
  else
    FINANCE_NODE="$FINANCE_ROOT/.runtime/node/current/bin/node"
    if ! financial_node_ok "$FINANCE_NODE" || ! "$FINANCE_NODE" "$FINANCE_ROOT/.runtime/node/current/lib/node_modules/npm/bin/npm-cli.js" --version >/dev/null 2>&1; then
      financial_install_node || return 1
    fi
    PATH="$FINANCE_ROOT/.runtime/node/current/bin:$PATH"
    npm_execpath="$FINANCE_ROOT/.runtime/node/current/lib/node_modules/npm/bin/npm-cli.js"
    export npm_execpath
    if [ -n "$financial_system_node" ] && financial_node_ok "$financial_system_node"; then
      FINANCE_NODE=$financial_system_node
      PATH="$(dirname -- "$FINANCE_NODE"):$PATH"
    fi
  fi
  FINANCE_BOOTSTRAPPED=1
  export FINANCE_NODE PATH FINANCE_BOOTSTRAPPED
}
