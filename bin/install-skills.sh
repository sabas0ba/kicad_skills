#!/usr/bin/env bash
# Wire this repository into a project, or into this checkout itself.
#
#   git submodule add https://github.com/sabas0ba/kicad_skills tools/kicad_skills
#   ./tools/kicad_skills/bin/install-skills.sh
#
# The guides live in docs/guides/ as ordinary Markdown - that is the source of
# truth, and nothing has to be installed to read them. This script exists for
# tools that want them in a particular layout, and for the CLI shim:
#
#   bin/eda.sh               -> a shim that runs the toolkit's wrapper, so the
#                               `./bin/eda.sh ...` commands in the docs work
#                               verbatim from the project root
#   .agents/skills/<name>/SKILL.md -> each guide in the tool-neutral Agent
#                                    Skills layout used by Codex and compatible
#                                    assistants
#   .claude/skills/<name>/SKILL.md -> the same guides for Claude Code
#
# Both layouts are installed by default. Point --dest somewhere else to install
# only that custom layout, or pass --no-guides if you only want the CLI.
#
# Options:
#   --dest DIR     install only into DIR (default: both standard layouts)
#   --copy         copy the guides instead of symlinking (vendoring, Windows)
#   --force        overwrite entries that already exist
#   --no-shim      skip bin/eda.sh
#   --no-guides    install only the shim
#   --uninstall    remove what this script installed
#   --target DIR   install into DIR (default: the git root above the submodule,
#                  or this repository itself when it is not a submodule)
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE=symlink
FORCE=0
SHIM=1
GUIDES=1
ACTION=install
TARGET=""
DEST=""

while [ $# -gt 0 ]; do
    case "$1" in
        --copy) MODE=copy ;;
        --force) FORCE=1 ;;
        --no-shim) SHIM=0 ;;
        --no-guides) GUIDES=0 ;;
        --dest) DEST="${2:?--dest needs a directory}"; shift ;;
        --uninstall) ACTION=uninstall ;;
        --target) TARGET="${2:?--target needs a directory}"; shift ;;
        -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "error: unknown option $1" >&2; exit 2 ;;
    esac
    shift
done

# ---- where does the project live ------------------------------------------
if [ -z "$TARGET" ]; then
    # As a submodule, install into the superproject; standalone, into this
    # checkout, so that working *on* the toolkit still gets the skills.
    TARGET="$(git -C "$SOURCE_ROOT" rev-parse --show-superproject-working-tree 2>/dev/null || true)"
    [ -z "$TARGET" ] && TARGET="$SOURCE_ROOT"
fi
TARGET="$(cd "$TARGET" && pwd)"
SELF_INSTALL=0
[ "$TARGET" = "$SOURCE_ROOT" ] && SELF_INSTALL=1

GUIDE_SRC="$SOURCE_ROOT/docs/guides"
DEST="$(printf '%s\n' "$DEST" | sed 's://*:/:g')"
DEST="${DEST#/}"; DEST="${DEST%/}"
[ -d "$GUIDE_SRC" ] || { echo "error: no guides in $GUIDE_SRC" >&2; exit 1; }

if [ -n "$DEST" ]; then
    DESTINATIONS=("$DEST")
else
    DESTINATIONS=(".agents/skills" ".claude/skills")
fi

INSTALL_MARKER=".eda-toolkit-installed"
INSTALL_MARKER_CONTENT="created by kicad_skills/bin/install-skills.sh"

# Path of the toolkit relative to the project, for relocatable symlinks.
if [ "$SELF_INSTALL" = 1 ]; then
    SUB_REL="."
else
    case "$SOURCE_ROOT" in
        "$TARGET"/*) SUB_REL="${SOURCE_ROOT#"$TARGET"/}" ;;
        *) SUB_REL="" ;;  # not nested: fall back to absolute links
    esac
fi

guide_names() {
    for guide in "$GUIDE_SRC"/*.md; do
        name="$(basename "$guide" .md)"
        [ "$name" = "README" ] && continue
        printf '%s\n' "$name"
    done
}

# ---- uninstall -------------------------------------------------------------
if [ "$ACTION" = uninstall ]; then
    for dest in "${DESTINATIONS[@]}"; do
        guide_dst="$TARGET/$dest"
        for name in $(guide_names); do
            skill_dir="${guide_dst:?}/$name"
            marker="$skill_dir/$INSTALL_MARKER"
            if [ ! -f "$marker" ] || [ "$(cat "$marker")" != "$INSTALL_MARKER_CONTENT" ]; then
                if [ -e "$skill_dir/SKILL.md" ] || [ -L "$skill_dir/SKILL.md" ]; then
                    echo "skip $dest/$name (not installed by this script)"
                fi
                continue
            fi
            rm -f "$skill_dir/SKILL.md" "$marker"
            rmdir "$skill_dir" 2>/dev/null || true
            echo "removed $dest/$name"
        done
        # Remove directories we created, innermost first, only while empty.
        dir="$guide_dst"
        while [ "$dir" != "$TARGET" ] && [ -d "$dir" ]; do
            rmdir "$dir" 2>/dev/null || break
            dir="$(dirname "$dir")"
        done
    done
    if [ -f "$TARGET/bin/eda.sh" ] && grep -q "install-skills.sh" "$TARGET/bin/eda.sh" 2>/dev/null; then
        rm -f "$TARGET/bin/eda.sh"
        echo "removed bin/eda.sh"
    fi
    exit 0
fi

# ---- install ---------------------------------------------------------------
if [ "$GUIDES" = 1 ]; then
    for dest in "${DESTINATIONS[@]}"; do
        guide_dst="$TARGET/$dest"
        # Levels up from guide_dst/<name>/ to TARGET, for relative links.
        IFS=/ read -r -a dest_parts <<< "$dest"
        up="../"
        for _ in "${dest_parts[@]}"; do up="../$up"; done
        for name in $(guide_names); do
            skill_dir="$guide_dst/$name"
            dst="$skill_dir/SKILL.md"
            marker="$skill_dir/$INSTALL_MARKER"
            if [ -e "$dst" ] || [ -L "$dst" ]; then
                if [ "$FORCE" != 1 ]; then
                    if [ ! -e "$marker" ] && [ ! -L "$marker" ] &&
                        [ -L "$dst" ] && [ "$dst" -ef "$GUIDE_SRC/$name.md" ]; then
                        printf '%s\n' "$INSTALL_MARKER_CONTENT" > "$marker"
                        echo "migrated $dest/$name (legacy symlink)"
                    else
                        echo "skip $dest/$name (already exists; --force to replace)"
                    fi
                    continue
                fi
                rm -f "$dst"
            fi
            mkdir -p "$skill_dir"
            if [ "$MODE" = copy ]; then
                cp -L "$GUIDE_SRC/$name.md" "$dst"
            elif [ -n "$SUB_REL" ]; then
                ln -s "$up$SUB_REL/docs/guides/$name.md" "$dst"
            else
                ln -s "$GUIDE_SRC/$name.md" "$dst"
            fi
            printf '%s\n' "$INSTALL_MARKER_CONTENT" > "$marker"
            echo "installed $dest/$name/SKILL.md ($MODE)"
        done
    done
fi

if [ "$SHIM" = 1 ] && [ "$SELF_INSTALL" = 0 ]; then
    mkdir -p "$TARGET/bin"
    shim="$TARGET/bin/eda.sh"
    if { [ ! -e "$shim" ] || [ "$FORCE" = 1 ]; }; then
        # Relative when nested, so the shim survives the project being moved.
        if [ -n "$SUB_REL" ]; then
            wrapper='$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)'"/$SUB_REL/bin/eda.sh"
        else
            wrapper="$SOURCE_ROOT/bin/eda.sh"
        fi
        cat > "$shim" <<EOF
#!/usr/bin/env bash
# Generated by kicad_skills/bin/install-skills.sh - runs the toolkit container.
# Regenerate with: $SUB_REL/bin/install-skills.sh --force
set -euo pipefail
exec "$wrapper" "\$@"
EOF
        chmod +x "$shim"
        echo "installed bin/eda.sh (shim)"
    else
        echo "skip bin/eda.sh (already exists; --force to replace)"
    fi
fi

if [ "$SELF_INSTALL" = 1 ]; then
    cat <<EOF

Done - the skill layouts now mirror docs/guides/ for this checkout. They are
git-ignored: docs/guides/ is the source of truth, these are only adapters.
EOF
else
    cat <<EOF

Done. Next:
  ./bin/eda.sh doctor          builds the image on first use, then reports versions
  ./bin/eda.sh report . -o build/report

Commit bin/eda.sh and any skill adapters your team chooses to track.
EOF
fi
