#!/usr/bin/env bash
# shellclaw — universal curl installer
# Usage: curl -fsSL https://raw.githubusercontent.com/MDFahimAnjum/shellclaw/main/install.sh | bash

set -euo pipefail

REPO="MDFahimAnjum/shellclaw"
INSTALL_DIR="${HOME}/.local/bin"
BIN_NAME="shellclaw"
GITHUB_API="https://api.github.com/repos/${REPO}/releases/latest"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[shellclaw]${NC} $*"; }
warn() { echo -e "${YELLOW}[warning]${NC}  $*"; }
die()  { echo -e "${RED}[error]${NC}    $*" >&2; exit 1; }

detect_arch() {
    local arch
    arch="$(uname -m)"
    case "${arch}" in
        x86_64)  echo "amd64" ;;
        aarch64) echo "arm64" ;;
        armv7l)  echo "armv7" ;;
        *)        die "Unsupported architecture: ${arch}" ;;
    esac
}

detect_os() {
    local os
    os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    case "${os}" in
        linux)  echo "linux" ;;
        darwin) echo "darwin" ;;
        *)       die "Unsupported OS: ${os}" ;;
    esac
}

check_dependency() {
    command -v "$1" >/dev/null 2>&1 || die "Required tool not found: $1. Please install it and retry."
}

# Returns 0 if user answered yes (y/yes), 1 otherwise. Defaults to no if no TTY.
ask_yes() {
    local prompt="$1"
    local reply reply_lc
    if [[ ! -r /dev/tty ]]; then
        return 1
    fi
    read -r -p "${prompt} [y/N] " reply < /dev/tty || return 1
    reply_lc="$(printf '%s' "${reply}" | tr '[:upper:]' '[:lower:]')"
    [[ "${reply_lc}" == y || "${reply_lc}" == yes ]]
}

optional_ollama_and_simplex() {
    if [[ ! -r /dev/tty ]]; then
        warn "No terminal for prompts: skipping optional Ollama and SimpleX steps."
        return 0
    fi

    if ask_yes "Install Ollama (local LLMs)?"; then
        log "Installing Ollama..."
        curl -fsSL https://ollama.com/install.sh | sh
        if ask_yes "Pull Ollama model qwen3.5:9b now? (large download)"; then
            if command -v ollama >/dev/null 2>&1; then
                ollama pull qwen3.5:9b || warn "Pull failed; run later: ollama pull qwen3.5:9b"
            else
                warn "ollama not in PATH in this shell yet. After restarting the terminal, run: ollama pull qwen3.5:9b"
            fi
        fi
    fi

    if ask_yes "Install SimpleX Chat (phone messaging)?"; then
        log "Installing SimpleX Chat..."
        curl -o- https://raw.githubusercontent.com/simplex-chat/simplex-chat/stable/install.sh | bash
    fi
}

main() {
    log "Starting shellclaw installer..."

    check_dependency curl
    optional_ollama_and_simplex

    log "Installing shellclaw..."

    check_dependency jq

    local arch os asset_name download_url
    arch="$(detect_arch)"
    os="$(detect_os)"
    asset_name="${BIN_NAME}-${os}-${arch}"

    log "Fetching latest release info..."
    local release_json
    release_json="$(curl -fsSL "${GITHUB_API}")"

    download_url="$(
        echo "${release_json}" \
        | jq -r ".assets[] | select(.name == \"${asset_name}\") | .browser_download_url"
    )"

    if [[ -z "${download_url}" || "${download_url}" == "null" ]]; then
        die "No binary found for ${os}/${arch}. Check https://github.com/${REPO}/releases"
    fi

    mkdir -p "${INSTALL_DIR}"

    local tmp_file
    tmp_file="$(mktemp)"
    trap "rm -f '${tmp_file}'" EXIT

    log "Downloading ${asset_name}..."
    curl -fsSL --progress-bar "${download_url}" -o "${tmp_file}"

    chmod +x "${tmp_file}"
    mv "${tmp_file}" "${INSTALL_DIR}/${BIN_NAME}"
    trap - EXIT

    log "Installed to ${INSTALL_DIR}/${BIN_NAME}"

    # Add to PATH if needed
    if ! echo "${PATH}" | grep -q "${INSTALL_DIR}"; then
        warn "${INSTALL_DIR} is not in your PATH."
        echo ""
        echo "  Add this line to your shell profile (~/.bashrc, ~/.zshrc, etc.):"
        echo ""
        echo "    export PATH=\"\${HOME}/.local/bin:\${PATH}\""
        echo ""
    fi

    log "Done! Run: ${BIN_NAME}"
}

main "$@"
