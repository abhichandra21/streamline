#compdef recommend

_recommend() {
    local -a subcommands
    subcommands=(
        'setup:Run offline setup (fetch metadata, build profile)'
    )

    local -a global_opts
    global_opts=(
        '--debug[Enable debug logging]'
        '--provider[LLM provider]:provider:(anthropic gemini)'
        '-n[Number of results]:count:'
        '--liked[Mark a title as liked]:title:'
        '--disliked[Mark a title as disliked]:title:'
        '--add[Add a title to watch history]:title:'
        '--type[Content type for --add]:type:(tv movie)'
        '--help[Show help]'
    )

    local -a setup_opts
    setup_opts=(
        '--refresh-data[Re-fetch TMDB metadata and rebuild everything]'
        '--refresh-profile[Rebuild taste profile only]'
        '--debug[Enable debug logging]'
        '--provider[LLM provider]:provider:(anthropic gemini)'
        '--help[Show help]'
    )

    if (( CURRENT == 2 )) && [[ "$words[2]" != -* ]]; then
        _describe 'subcommand' subcommands
        _arguments '*:query:'
        return
    fi

    if [[ "$words[2]" == "setup" ]]; then
        _arguments $setup_opts
    else
        _arguments $global_opts '*:query:'
    fi
}

_recommend_web() {
    local -a commands
    commands=(
        'start:Start the web UI server'
        'stop:Stop the web UI server'
        'status:Check if the server is running'
        'restart:Restart the web UI server'
        'logs:View server logs'
    )
    _describe 'command' commands
}

compdef _recommend ./recommend recommend
compdef _recommend_web ./recommend-web recommend-web
