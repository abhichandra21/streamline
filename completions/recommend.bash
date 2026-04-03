#!/bin/bash
# Bash completion for ./recommend and ./recommend-web

_recommend_completion() {
    local cur prev
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    # Check if "setup" subcommand is active
    local has_setup=false
    for word in "${COMP_WORDS[@]}"; do
        if [[ "$word" == "setup" ]]; then
            has_setup=true
            break
        fi
    done

    if $has_setup; then
        local setup_opts="--refresh-data --refresh-profile --debug --provider --help"
        case "$prev" in
            --provider)
                COMPREPLY=( $(compgen -W "anthropic gemini" -- "$cur") )
                return 0
                ;;
        esac
        COMPREPLY=( $(compgen -W "$setup_opts" -- "$cur") )
        return 0
    fi

    case "$prev" in
        --provider)
            COMPREPLY=( $(compgen -W "anthropic gemini" -- "$cur") )
            return 0
            ;;
        --type)
            COMPREPLY=( $(compgen -W "tv movie" -- "$cur") )
            return 0
            ;;
    esac

    if [[ "$cur" == -* ]]; then
        local opts="--debug --provider -n --liked --disliked --add --type --help"
        COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
    elif [[ "$COMP_CWORD" == 1 ]]; then
        COMPREPLY=( $(compgen -W "setup history" -- "$cur") )
    fi
}

_recommend_web_completion() {
    local cur
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    COMPREPLY=( $(compgen -W "start stop status restart logs" -- "$cur") )
}

complete -F _recommend_completion recommend ./recommend
complete -F _recommend_web_completion recommend-web ./recommend-web
