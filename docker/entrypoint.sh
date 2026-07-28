#!/usr/bin/env bash
# Container entrypoint: make HOME writable for arbitrary --user ids, seed the
# KiCad library tables, then run the requested command.
set -euo pipefail

if [ ! -w "${HOME:-/nonexistent}" ]; then
    export HOME=/tmp/eda-home
fi
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
mkdir -p "$HOME" "${MPLCONFIGDIR:-$HOME/matplotlib}" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" 2>/dev/null || true

# kicad-cli only reads the global symbol/footprint library tables; without them
# every footprint reports "library not found" in DRC. The GUI seeds them on first
# start, so do the same here.
seed_library_tables() {
    local template_dir=/usr/share/kicad/template
    [ -d "$template_dir" ] || return 0
    local version_dir
    version_dir="$(kicad-cli version 2>/dev/null | cut -d. -f1,2)"
    [ -n "$version_dir" ] || return 0
    local config_dir="$XDG_CONFIG_HOME/kicad/$version_dir"
    mkdir -p "$config_dir" 2>/dev/null || return 0
    local table
    for table in fp-lib-table sym-lib-table design-block-lib-table; do
        if [ -f "$template_dir/$table" ] && [ ! -f "$config_dir/$table" ]; then
            cp "$template_dir/$table" "$config_dir/$table" 2>/dev/null || true
        fi
    done
}
seed_library_tables

case "${1:-}" in
    bash|sh|python3|python|pytest|ngspice|kicad-cli|make)
        exec "$@"
        ;;
    "")
        exec eda doctor
        ;;
    *)
        exec eda "$@"
        ;;
esac
