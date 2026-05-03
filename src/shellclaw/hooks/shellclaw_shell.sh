# shellclaw — sourced by bash (via --rcfile) or zsh (via ZDOTDIR .zshrc).
# Emits invisible OSC markers for deterministic command boundaries.

# Inner bash/zsh does not load this file.  Without the synthetic END below, OSC
# START would stay open until the inner shell exits (Stop button stuck).

__shellclaw_trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

__shellclaw_is_interactive_shell_only() {
    local t
    t="$(__shellclaw_trim "$1")"
    case "$t" in
        *-c*|*"&&"*|*"||"*|*"|"*|*\;*)
            return 1 ;;
    esac
    case "$t" in
        bash|bash\ -l|bash\ --login|bash\ -i|bash\ --norc)
            return 0 ;;
        exec\ bash|exec\ bash\ -l|exec\ bash\ --login|exec\ bash\ -i)
            return 0 ;;
        command\ bash|command\ bash\ -l)
            return 0 ;;
        /bin/bash|/usr/bin/bash|/usr/local/bin/bash)
            return 0 ;;
        env\ bash|/usr/bin/env\ bash)
            return 0 ;;
        zsh|zsh\ -l|zsh\ -i|zsh\ --login)
            return 0 ;;
        exec\ zsh|exec\ zsh\ -l|command\ zsh)
            return 0 ;;
        /bin/zsh|/usr/bin/zsh|/usr/local/bin/zsh)
            return 0 ;;
        sh|/bin/sh|/usr/bin/sh)
            return 0 ;;
        dash|/bin/dash|/usr/bin/dash)
            return 0 ;;
        fish|/usr/bin/fish|/bin/fish)
            return 0 ;;
        ksh|mksh|/bin/ksh)
            return 0 ;;
        exec\ sh|exec\ dash|exec\ fish)
            return 0 ;;
    esac
    return 1
}

__shellclaw_preexec() {
    printf '\033]777;shellclaw_START;cmd=%s\007' "$1"
    if __shellclaw_is_interactive_shell_only "$1"; then
        printf '\033]777;shellclaw_END;exit=0\007'
    fi
}

__shellclaw_precmd() {
    local ec=$?
    printf '\033]777;shellclaw_END;exit=%d\007' "$ec"
}

# Bash runs the DEBUG trap before every simple command, including each clause in
# PROMPT_COMMAND.  One OSC END per prompt would then follow multiple OSC STARTs
# (depth never returns to 0 → Stop button stuck).  Run the user's prompt hooks
# behind a guard so DEBUG does not emit START for prompt-time commands.

__shellclaw_in_prompt_command=0

__shellclaw_prompt_command() {
    __shellclaw_in_prompt_command=1
    __shellclaw_precmd
    if [ -n "${__shellclaw_saved_prompt_command:-}" ]; then
        eval "$__shellclaw_saved_prompt_command"
    fi
    __shellclaw_in_prompt_command=0
}

__shellclaw_debug_trap() {
    if [ "${__shellclaw_in_prompt_command:-0}" -ne 0 ]; then
        return 0
    fi
    case $BASH_COMMAND in
        __shellclaw_prompt_command)
            return 0 ;;
    esac
    __shellclaw_preexec "$BASH_COMMAND"
}

if [ -n "${BASH_VERSION:-}" ]; then
    __shellclaw_saved_prompt_command="${PROMPT_COMMAND:-}"
    PROMPT_COMMAND="__shellclaw_prompt_command"
    trap '__shellclaw_debug_trap' DEBUG
fi

if [ -n "${ZSH_VERSION:-}" ]; then
    autoload -Uz add-zsh-hook 2>/dev/null || true
    if whence -w add-zsh-hook >/dev/null 2>&1; then
        add-zsh-hook preexec __shellclaw_preexec
        add-zsh-hook precmd __shellclaw_precmd
    fi
fi

# --- Nested bash/zsh: re-exec with the same session rc files (SHELLCLAW_* from rc snippets).
# Keeps OSC hooks active when switching shells inside the TUI.  ``command bash`` or a full
# path bypasses these wrappers.

__shellclaw_nested_bash_argv_ok() {
    while [ $# -gt 0 ]; do
        case "$1" in
            -c|--norc|--rcfile|--) return 1 ;;
            -i|-l|-il|-li|--login|--interactive) shift ;;
            -o) return 1 ;;
            -*)
                _sw="${1#-}"
                case "$_sw" in
                    ""|*[^il]*|--*) return 1 ;;
                esac
                shift ;;
            *) return 1 ;;
        esac
    done
    return 0
}

__shellclaw_nested_zsh_argv_ok() {
    while [ $# -gt 0 ]; do
        case "$1" in
            -c|-fc|-f|--no-rcs|--no-globalrcs|--help|-V|--version|--) return 1 ;;
            -o) return 1 ;;
            -i|-l|-il|-li|--login) shift ;;
            -*)
                _sw="${1#-}"
                case "$_sw" in
                    ""|*[^il]*|--*) return 1 ;;
                esac
                shift ;;
            *) return 1 ;;
        esac
    done
    return 0
}

bash() {
    if [ -z "${SHELLCLAW_BASHRC:-}" ] || [ ! -f "${SHELLCLAW_BASHRC}" ]; then
        command bash "$@"
        return
    fi
    if __shellclaw_nested_bash_argv_ok "$@"; then
        command bash --rcfile "${SHELLCLAW_BASHRC}" -i "$@"
    else
        command bash "$@"
    fi
}

zsh() {
    if [ -z "${SHELLCLAW_ZDOTDIR:-}" ] || [ ! -d "${SHELLCLAW_ZDOTDIR}" ]; then
        command zsh "$@"
        return
    fi
    if __shellclaw_nested_zsh_argv_ok "$@"; then
        ZDOTDIR="${SHELLCLAW_ZDOTDIR}" command zsh -i "$@"
    else
        command zsh "$@"
    fi
}
