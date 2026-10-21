#!/bin/bash

# Log Archive Tool
# SCRIPT NAME: log-archive.sh
# Description: Simple log archive tool that creates a timestamped tar.gz archive of all contents
#              in the specified directory, including hidden files. Archives are saved in
#              "$HOME/log-archives", verified before publication, in 
#               "$HOME/log-archives/archive.log". Original source files are never deleted.
# AUTHOR:      OpsByNyiko

set -euo pipefail
umask 077

# Configuration

ARCHIVE_DIR="${HOME:?Error: Home is not set}/log-archives"
LOG_FILE="$ARCHIVE_DIR/archive-log"

# Track temporary archive to cleanup on failure
TEMP_ARCHIVE=""

# Logging

log_event(){
    local status = "$1"
    local message = "$2"
    local timestamp

    timestamp = $(date '+%Y-%m-%d %H:%M:%S') || {
        printf 'ERROR: Unable to obtain timestamp.\n' >&2
        return 1
    }

    if ! printf '%s | %s | %s\n' \
    "$timestamp" "$status" "$message" >> "$LOG_FILE"; then
    printf 'ERROR: Unable to write to %\n' "$LOG_FILE" >&2
    return 1
    fi
}

fail() {
    local message="$1"

    printf 'ERROR: %s\n' "$message" >&2
    log_event "FAILURE" "$message" || true
    exit 1
}

cleanup(){
    local message = "$1"

    # Prevent recursive exit trap execution
    trap - EXIT

    if [[ -n "$TEMP_ARCHIVE" && -e "$TEMP_ARCHIVE" ]]; then
       if ! rm -f -- "$TEMP_ARCHIVE"; then
       printf 'WARNING: Could not remove temporary file: s%n\' \
            "$TEMP_ARCHIVE" >&2
       exit_status=1
       fi
    fi

    exit "$exit_status"
}

handle_signal() {
    local signal="$1"

    log_event "FAILURE" "Archive interrupted by $signal" || true
    exit 1
}

# Input Validation

usage(){
    cat <<'EOF'
Usage: log-archive.sh <log-directory>

Arguments:
   <log-directory> Directory whose contents will be archived

Archives:
   $HOME/log-archives/logs_archive_YYYYMMDD_HHMMSS.tar.gz

 Original Files are preserved
EOF
}

validate_input(){
    if [[ "$#" -ne 1 ]]; then
        usage >&2
        exit 2
    fi

    if [[ ! -d "$1" ]]; then
        fail "Source is not an existing directory: $1"
    fi

    if [[ ! -r "$1" || ! -x "$1" ]]; then
        fail "Source directory must be readable and searchable: $1"
    fi
}

# Dependency Checks

check_dependencies(){
    local utility

    for utility in tar gzip awk date mkdir mktemp ln rm touch du; do
       if ! command -v "$utility" >/dev/null 2>&1; then
           fail "Required utility is missing: $utility"
       fi
    done
}

# Archive Setup

setup_archive_dir(){
    if ! mkdir -p -- "$ARCHIVE_DIR"; then
       printf 'ERROR: Cannot create archive directory: %s\n' \
           "$ARCHIVE_DIR" >&2
       exit 1
    fi

    if [[ ! -d "$ARCHIVE_DIR" || ! -w "$ARCHIVE_DIR" ||
          ! -x "$ARCHIVE_DIR" ]]; then
        printf 'ERROR: Archive directory is not writable: %s\n' \
            "$ARCHIVE_DIR" >&2
        exit 1
    fi

    # Resolve the destination path
    ARCHIVE_DIR=$(cd -- "$ARCHIVE_DIR" && pwd -P) || exit 1
    LOG_FILE="$ARCHIVE_DIR/archive.log"
}

# Create Archive

create_archive(){
    local source_dir="$1"
    local timestamp
    local archive_name
    local final_archive
    local archive_size

    timestamp=$(date '+%Y%m%d_%H%M%S') ||
        fail "Could not generate archive timestamp."

    archive_name="logs_archive_${timestamp}.tar.gz"
    final_archive="$ARCHIVE_DIR/$archive_name"

    # Do not overwrite an existing archive with the same timestamp.
    if [[ -e "$final_archive" || -L "$final_archive" ]]; then
        fail "Archive already exists; can't overwrite: $final_archive"
    fi

    # Create a temporary file in the destination directory to allow publication
    #    without crossing filesystems.
    TEMP_ARCHIVE=$(mktemp "$ARCHIVE_DIR/.archive.tmp.XXXXXX") ||
        fail "Could not create a temporary archive file."

    printf 'Source:      %s\n' "$source_dir"
    printf 'Destination: %s\n' "$final_archive"
    printf 'Creating compressed archive...\n'

    # Archive the contents, including hidden files, without
    # embedding the source directory's absolute path.
    if ! tar -czf "$TEMP_ARCHIVE" -C "$source_dir" .; then
        fail "Compression failed for source directory: $source_dir"
    fi

    printf 'Verifying archive integrity...\n'

    # Test gzip integrity and confirm tar can read the archive.
    if ! gzip -t "$TEMP_ARCHIVE" ||
       ! tar -tzf "$TEMP_ARCHIVE" >/dev/null; then
        fail "Archive integrity verification failed."
    fi

    # Hard-link publication is atomic and refuses to overwrite
    # an existing destination. The temporary file is on the same
    # filesystem as the final archive.
    if ! ln -- "$TEMP_ARCHIVE" "$final_archive"; then
        fail "Could not publish archive: $final_archive"
    fi

    # The final archive now exists; remove the temporary name.
    if ! rm -f -- "$TEMP_ARCHIVE"; then
        printf 'WARNING: Archive published, but temporary cleanup failed.\n' >&2
    fi
    TEMP_ARCHIVE=""

    archive_size=$(du -h "$final_archive" 2>/dev/null | awk '{print $1}')
    archive_size="${archive_size:-unknown}"

    if ! log_event "SUCCESS" \
        "Source=$source_dir | Archive=$final_archive | Size=$archive_size"; then
        printf 'WARNING: Archive exists, but its success event was not logged.\n' >&2
        return 1
    fi

    printf '\nArchive created and verified successfully.\n'
    printf 'Archive: %s\n' "$final_archive"
    printf 'Size:    %s\n' "$archive_size"
    printf 'Log:     %s\n' "$LOG_FILE"
}

# Main Execution

main(){
    local source_dir

    # Check the argument count before performing any work.
    if [[ "$#" -ne 1 ]]; then
        usage >&2
        exit 2
    fi

    # Set up logging early so subsequent validation failures can
    # be recorded whenever the destination is available.
    setup_archive_dir
    trap cleanup EXIT
    trap 'handle_signal SIGINT' INT
    trap 'handle_signal SIGTERM' TERM

    validate_input "$@"
    check_dependencies

    # Resolve relative paths and remove trailing path components.
    source_dir=$(cd -- "$1" && pwd -P) ||
        fail "Could not resolve source directory: $1"

    # Prevent the archive directory from being inside the source.
    # Otherwise the archive could include its own output.
    case "$ARCHIVE_DIR/" in
        "$source_dir/"*)
            fail "Archive destination must not be inside the source directory."
            ;;
    esac

    # Ensure the event log can be written before starting.
    if ! touch "$LOG_FILE"; then
        printf 'ERROR: Cannot create or write to event log: %s\n' \
            "$LOG_FILE" >&2
        exit 1
    fi

    create_archive "$source_dir"
}

main "$@"


