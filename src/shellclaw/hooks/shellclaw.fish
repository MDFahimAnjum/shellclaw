# shellclaw — fish init hooks (source with fish --init-command).

function __shellclaw_interactive_shell_only -a cmd
    set -l t (string trim -- $cmd)
    if string match -q '*-c*' -- $t
        return 1
    end
    if string match -q '*&&*' -- $t; or string match -q '*||*' -- $t; or string match -q '*|*' -- $t; or string match -q '*;*' -- $t
        return 1
    end
    switch $t
        case bash 'bash -l' 'bash --login' 'bash -i' 'bash --norc' \
            'exec bash' 'exec bash -l' 'exec bash -i' \
            'command bash' 'command bash -l' \
            /bin/bash /usr/bin/bash /usr/local/bin/bash \
            'env bash' '/usr/bin/env bash' \
            zsh 'zsh -l' 'zsh -i' 'zsh --login' \
            'exec zsh' 'exec zsh -l' 'command zsh' \
            /bin/zsh /usr/bin/zsh /usr/local/bin/zsh \
            sh /bin/sh /usr/bin/sh \
            dash /bin/dash /usr/bin/dash \
            fish /usr/bin/fish /bin/fish \
            ksh mksh /bin/ksh \
            'exec sh' 'exec dash' 'exec fish'
            return 0
        case '*'
            return 1
    end
end

function __shellclaw_preexec_fish --on-event fish_preexec
    set -l line (string join ' ' $argv)
    printf '\033]777;shellclaw_START;cmd=%s\007' $line
    if __shellclaw_interactive_shell_only $line
        printf '\033]777;shellclaw_END;exit=0\007'
    end
end

function __shellclaw_postexec_fish --on-event fish_postexec
    printf '\033]777;shellclaw_END;exit=%d\007' $status
end
