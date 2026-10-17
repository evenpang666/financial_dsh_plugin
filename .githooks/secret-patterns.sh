#!/bin/sh
# Heuristic checks only. Never print matching credential values.
secret_pattern=$(cat <<'PATTERN'
sk-(proj-|ant-api[0-9]+-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|(AKIA|ASIA)[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{20,}|-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}|["']?([A-Za-z0-9_]*_)?(api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|secret[_-]?key|password|passwd|token)["']?[[:space:]]*[:=][[:space:]]*["']?[A-Za-z0-9_+/=-]{32,}|https?://[^ /:"'<>]+:[^ /@"'<>]{6,}@
PATTERN
)

check_secret_content() {
    git grep -l -I -i -E -e "$secret_pattern" "$@" -- .
    result=$?
    case "$result" in
        0) echo "Blocked: possible credential content in the files above; values are redacted. Use environment variables or local credential files." >&2; return 1 ;;
        1) return 0 ;;
        *) echo "Blocked: credential scan could not finish." >&2; return 1 ;;
    esac
}
