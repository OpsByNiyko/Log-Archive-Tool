#!/usr/bin/env python3

""" Python implementation of the bash log archive tool that compresses a log-directory into
a timestamped tar.gz archive.

Usage:
      log-archive.py <log-directory>
      
Archives are written to ``~/log-archives/logs_archive_YYYYMMDD_HHMMSS.tar.gz``

and every run is recorded in ``~/log-archives/archive.log``.
The source directory is never modified or deleted.
"""

from __future__ import annotations
 
import argparse
import os
import signal
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence


# Configuration

ARCHIVE_DIR_NAME= "log-archives"
EVENT_LOG_NAME= "archive.log"
ARCHIVE_PREFIX= "logs_archive"
ARCHIVE_SUFFIX= ".tar.gz"
TEMP_PREFIX= ".archive.tmp."

FILE_TIMESTAMP_FORMAT= "%Y%m%d_%H%M%S"
EVENT_TIMESTAMP_FORMAT= "%Y-%m-%d %H:%M:%S"

EXIT_OK= 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

# Errors and Models

class ArchiveError(Exception):
    """Base class for every expected, user-reportable failure."""
    
class InterruptedBySignal(ArchiveError):
    """Raised from a signal handler so cleanup code can run."""

@dataclass(frozen=True)
class ArchiveResult:
    """Outcome of a successful archive run."""
    
    source: Path
    archive: Path
    size_bytes: int
    
# Helpers

def format_size(num_bytes: int) -> str:
    """Return a human-readable size such as ``12.4 MiB``."""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def resolve_source(raw_path: str) -> Path:
    """Validate the user-supplied directory and return its absolute path."""
    path = Path(raw_path).expanduser()
    if not path.is_dir():
        raise ArchiveError(f"Source is not an existing directory: {raw_path}")
    if not os.access(path, os.R_OK | os.X_OK):
        raise ArchiveError(
            f"Source directory must be readable and searchable: {raw_path}"
        )
    return path.resolve()

def prepare_archive_dir(path: Path) -> Path:
    """Create the destination directory if needed and ensure it is writable."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ArchiveError(f"Cannot create archive directory: {path} ({exc})")
    if not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
        raise ArchiveError(f"Archive directory is not writable: {path}")
    return path.resolve()

# Event Logger

class EventLogger:
    """Appends ``timestamp | STATUS | message`` lines to the event log."""
 
    def __init__(
        self, log_file: Path, clock: Callable[[], datetime] = datetime.now
    ) -> None:
        self._log_file = log_file
        self._clock = clock
 
    @property
    def path(self) -> Path:
        return self._log_file
 
    def record(self, status: str, message: str) -> None:
        stamp = self._clock().strftime(EVENT_TIMESTAMP_FORMAT)
        try:
            with self._log_file.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp} | {status} | {message}\n")
        except OSError as exc:
            raise ArchiveError(f"Unable to write to {self._log_file}: {exc}")
        
# Archive Service

class ArchiveService:
    """Builds, verifies and atomically publishes a compressed archive."""
 
    def __init__(
        self,
        archive_dir: Path,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._archive_dir = archive_dir
        self._clock = clock
 
    def run(self, source: Path) -> ArchiveResult:
        self._ensure_destination_outside(source)
        final_path = self._next_archive_path()
 
        print(f"Source:      {source}")
        print(f"Destination: {final_path}")
 
        temp_path = self._create_temp_file()
        try:
            print("Creating compressed archive...")
            self._compress(source, temp_path)
 
            print("Verifying archive integrity...")
            self._verify(temp_path)
 
            self._publish(temp_path, final_path)
        finally:
            # Runs on success (removes the spare hard link) and on any failure
            # or signal (removes the partial archive).
            temp_path.unlink(missing_ok=True)
 
        return ArchiveResult(source, final_path, final_path.stat().st_size)
    
        # Steps
        
    def _ensure_destination_outside(self, source: Path) -> None:
        if self._archive_dir.is_relative_to(source):
            raise ArchiveError(
                "Archive destination must not be inside the source directory."
            )
 
    def _next_archive_path(self) -> Path:
        stamp = self._clock().strftime(FILE_TIMESTAMP_FORMAT)
        path = self._archive_dir / f"{ARCHIVE_PREFIX}_{stamp}{ARCHIVE_SUFFIX}"
        if path.exists() or path.is_symlink():
            raise ArchiveError(f"Archive already exists; refusing to overwrite: {path}")
        return path
    
    def _create_temp_file(self) -> Path:
        # Same directory as the final archive so the publish step never
        # crosses a filesystem boundary. mkstemp creates the file as 0600.
        try:
            fd, name = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=self._archive_dir)
        except OSError as exc:
            raise ArchiveError(f"Could not create a temporary archive file: {exc}")
        os.close(fd)
        return Path(name)
    
    @staticmethod
    def _compress(source: Path, destination: Path) -> None:
        # arcname="." stores paths relative to the source (no absolute paths)
        # and tarfile recurses into hidden files and directories as well.
        try:
            with tarfile.open(destination, "w:gz") as tar:
                tar.add(source, arcname=".")
        except (OSError, tarfile.TarError) as exc:
            raise ArchiveError(f"Compression failed for {source}: {exc}")
        
    @staticmethod
    def _verify(archive: Path) -> None:
        # Walking every member forces the whole gzip stream to be decompressed,
        # which also validates its CRC.
        try:
            with tarfile.open(archive, "r:gz") as tar:
                for _ in tar:
                    pass
        except (OSError, EOFError, tarfile.TarError) as exc:
            raise ArchiveError(f"Archive integrity verification failed: {exc}")
        
    @staticmethod
    def _publish(temp_path: Path, final_path: Path) -> None:
        # A hard link is atomic and, unlike rename, refuses to replace an
        # existing destination.
        try:
            os.link(temp_path, final_path)
        except OSError as exc:
            raise ArchiveError(f"Could not publish archive: {final_path} ({exc})")
        
# CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archive-log",
        description="Compress a log directory into a timestamped tar.gz archive.",
        epilog=(
            f"Archives are stored in ~/{ARCHIVE_DIR_NAME}/ and original "
            "files are preserved."
        ),
    )
    parser.add_argument(
        "log_directory",
        metavar="<log-directory>",
        help="directory whose contents will be archived (e.g. /var/log)",
    )
    return parser
    
def _raise_on_signal(signum: int, _frame: object) -> None:
    raise InterruptedBySignal(f"Archive interrupted by {signal.Signals(signum).name}")
 
 
def install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _raise_on_signal)
    signal.signal(signal.SIGTERM, _raise_on_signal)
 
 
def _fail(logger: EventLogger | None, message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    if logger is not None:
        try:
            logger.record("FAILURE", message)
        except ArchiveError:
            pass  # the original error is the one worth reporting
    return EXIT_FAILURE

def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.umask(0o077)  # archives may contain sensitive log data
    install_signal_handlers()
 
    logger: EventLogger | None = None
    try:
        archive_dir = prepare_archive_dir(Path.home() / ARCHIVE_DIR_NAME)
        logger = EventLogger(archive_dir / EVENT_LOG_NAME)
 
        source = resolve_source(args.log_directory)
        result = ArchiveService(archive_dir).run(source)
    except ArchiveError as exc:
        return _fail(logger, str(exc))
    except RuntimeError as exc:  # Path.home() when no home directory exists
        return _fail(logger, f"Could not determine home directory: {exc}")
 
    size = format_size(result.size_bytes)
    try:
        logger.record(
            "SUCCESS",
            f"Source={result.source} | Archive={result.archive} | Size={size}",
        )
    except ArchiveError as exc:
        print(f"WARNING: Archive exists, but its success event was not logged: {exc}",
              file=sys.stderr)
        return EXIT_FAILURE
 
    print("\nArchive created and verified successfully.")
    print(f"Archive: {result.archive}")
    print(f"Size:    {size}")
    print(f"Log:     {logger.path}")
    return EXIT_OK
 
 
if __name__ == "__main__":
    sys.exit(main())
