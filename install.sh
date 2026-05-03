#!/usr/bin/env bash
# shellclaw — universal curl installer
# Usage: curl -fsSL https://raw.githubusercontent.com/MDFahimAnjum/shellclaw/main/install.sh | bash

set -euo pipefail

REPO="MDFahimAnjum/shellclaw"
INSTALL_DIR="${HOME}/.local/bin"
BIN_NAME="shellclaw"
GITHUB_API="https://api.github.com/repos/${REPO}/releases/latest"
OLLAMA_MODEL_QWEN="qwen3.5:9b"
OLLAMA_MODEL_GEMMA="gemma4:e4b"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'

show_banner() {
    local bar="${GREEN}════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "${bar}"
    # ASCII art: output of `figlet -f small ShellClaw` (embedded so figlet is not required).
    while IFS= read -r line || [[ -n "${line}" ]]; do
        echo -e "${GREEN}${line}${NC}"
    done <<'BANNER'
   ___ _        _ _  ___ _             
  / __| |_  ___| | |/ __| |__ ___ __ __
  \__ \ ' \/ -_) | | (__| / _` \ V  V /
  |___/_||_\___|_|_|\___|_\__,_|\_/\_/ 
                                      
BANNER
    echo -e "  ${DIM}·${NC}  ${CYAN}universal installer${NC}  ${DIM}(curl | bash)${NC}"
    echo -e "${bar}"
    echo ""
}

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

# Returns 0 if ollama list reports this exact model name (first column).
ollama_list_has_model() {
    local name="$1"
    [[ -n "${name}" ]] || return 1
    ollama list 2>/dev/null | awk -v pat="${name}" 'NR > 1 && $1 == pat { found = 1 } END { exit(found ? 0 : 1) }'
}

optional_ollama_model_choice() {
    if ! command -v ollama >/dev/null 2>&1; then
        warn "ollama not in PATH in this shell yet. After restarting the terminal: ollama pull ${OLLAMA_MODEL_QWEN} or ollama pull ${OLLAMA_MODEL_GEMMA}"
        return 0
    fi

    local has_qwen=0 has_gemma=0
    if ollama_list_has_model "${OLLAMA_MODEL_QWEN}"; then
        has_qwen=1
    fi
    if ollama_list_has_model "${OLLAMA_MODEL_GEMMA}"; then
        has_gemma=1
    fi

    if [[ "${has_qwen}" -eq 1 && "${has_gemma}" -eq 1 ]]; then
        log "Ollama already has ${OLLAMA_MODEL_QWEN} and ${OLLAMA_MODEL_GEMMA}; skipping model download."
        return 0
    fi

    local label_q="${OLLAMA_MODEL_QWEN}"
    local label_g="${OLLAMA_MODEL_GEMMA}"
    [[ "${has_qwen}" -eq 1 ]] && label_q+=" (already installed)"
    [[ "${has_gemma}" -eq 1 ]] && label_g+=" (already installed)"

    local choice
    while true; do
        echo ""
        echo "Choose a model to pull for ShellClaw (large download), or skip:"
        echo "  1) ${label_q}"
        echo "  2) ${label_g}"
        echo "  3) Skip"
        read -r -p "Enter 1–3: " choice < /dev/tty || return 0
        case "${choice}" in
            1)
                if [[ "${has_qwen}" -eq 1 ]]; then
                    log "Model ${OLLAMA_MODEL_QWEN} is already present."
                else
                    log "Pulling ${OLLAMA_MODEL_QWEN}..."
                    ollama pull "${OLLAMA_MODEL_QWEN}" || warn "Pull failed; run later: ollama pull ${OLLAMA_MODEL_QWEN}"
                fi
                return 0
                ;;
            2)
                if [[ "${has_gemma}" -eq 1 ]]; then
                    log "Model ${OLLAMA_MODEL_GEMMA} is already present."
                else
                    log "Pulling ${OLLAMA_MODEL_GEMMA}..."
                    ollama pull "${OLLAMA_MODEL_GEMMA}" || warn "Pull failed; run later: ollama pull ${OLLAMA_MODEL_GEMMA}"
                fi
                return 0
                ;;
            3)
                log "Skipping model download."
                return 0
                ;;
            *)
                warn "Invalid choice; use 1, 2, or 3."
                ;;
        esac
    done
}

simplex_chat_installed() {
    command -v simplex-chat >/dev/null 2>&1
}

optional_ollama_and_simplex() {
    if [[ ! -r /dev/tty ]]; then
        warn "No terminal for prompts: skipping optional Ollama and SimpleX steps."
        return 0
    fi

    if command -v ollama >/dev/null 2>&1; then
        log "Ollama is already installed; skipping Ollama install."
        optional_ollama_model_choice
    elif ask_yes "[Step 1/2] Want to install Ollama for using local LLMs with ShellClaw?"; then
        log "Installing Ollama..."
        curl -fsSL https://ollama.com/install.sh | sh
        optional_ollama_model_choice
    fi

    if simplex_chat_installed; then
        log "SimpleX Chat is already installed; skipping SimpleX install."
    elif ask_yes "[Step 2/2] Want to install SimpleX Chat for using ShellClaw on your phone?"; then
        log "Installing SimpleX Chat..."
        curl -o- https://raw.githubusercontent.com/simplex-chat/simplex-chat/stable/install.sh | bash
    fi
}

main() {
    show_banner
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
