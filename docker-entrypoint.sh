#!/bin/sh
# Apply PUID/PGID, fix ownership of the writable mounts, then drop privileges.
#
# Started as root (the image default), this remaps the capforge user, chowns the
# caps/state directories, and execs the command as capforge. Started with
# `--user`, it cannot remap and simply runs the command as that user.
set -eu

PUID="${PUID:-10001}"
PGID="${PGID:-10001}"

valid_id() {
    case "$1" in
        '' | *[!0-9]*) return 1 ;;
    esac
    [ "$1" -gt 0 ]
}

if ! valid_id "$PUID"; then
    echo "PUID must be a positive integer, got '$PUID'" >&2
    exit 1
fi
if ! valid_id "$PGID"; then
    echo "PGID must be a positive integer, got '$PGID'" >&2
    exit 1
fi

if [ "$(id -u)" = "0" ]; then
    # Both commands are idempotent, and --non-unique allows host IDs that
    # already exist inside the image. usermod also updates the primary GID so
    # /etc/passwd stays consistent with the renamed group.
    groupmod --non-unique --gid "$PGID" capforge
    usermod --non-unique --uid "$PUID" --gid "$PGID" capforge

    caps_dir="${CAPFORGE_CAPS_DIR:-/home/capforge/.agents/skills}"
    state_dir="${CAPFORGE_STATE_DIR:-/var/lib/capforge}"

    for dir in /home/capforge "$caps_dir" "$state_dir"; do
        mkdir -p "$dir" 2>/dev/null || true
        if ! chown -R capforge:capforge "$dir" 2>/dev/null; then
            echo "warning: could not chown $dir to $PUID:$PGID" >&2
        fi
    done

    exec setpriv --reuid=capforge --regid=capforge --init-groups "$@"
fi

# A user-supplied --user wins; ownership of the mounts is the caller's job.
exec "$@"
