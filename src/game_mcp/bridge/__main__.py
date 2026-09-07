"""``sts-state-dump``: the advisor-mode state dumper.

Launched by CommunicationMod (inside the game). It prints ``ready`` once on stdout,
then reads stdin line by line, each non-empty line being one full game state pushed
as JSON. Each push becomes the newest snapshot and is written to ``latest.json`` (plus
a small ``latest.meta.json`` for cheap change detection); with ``--record-samples`` each
push is also archived. Nothing is ever sent back to the game except the ``ready``
handshake and, when the MCP server places ``state.request`` in the dump directory, the
read-only ``STATE`` query (the mod re-sends the current state). The dump cannot act on
the game.

stdout is reserved for the handshake; all logging goes to stderr and to a rotating file
``bridge.log`` in the output directory. EOF on stdin exits cleanly. stdin is read as bytes
and decoded per line (UTF-8 first, then the locale code page; ``--stdin-encoding`` forces
one), so a charset mismatch with the game JVM degrades to replacement characters instead
of killing the dump. On start the sequence number continues from ``latest.meta.json`` so
a game restart does not reset ``seq``.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from dataclasses import dataclass
from typing import Iterable, TextIO

from game_mcp.bridge import persist, protocol
from game_mcp.bridge.request import RequestWatcher
from game_mcp.bridge.protocol import LineDecoder, SnapshotStore

logger = logging.getLogger("game_mcp.bridge")

_LOG_MAX_BYTES = 1_000_000
_LOG_BACKUPS = 3
DEFAULT_SAMPLES_KEEP = 500
DEFAULT_SAMPLES_MAX_AGE_H = 72.0


@dataclass(frozen=True)
class SampleRetention:
    """Retention for ``--record-samples``: newest ``keep_files`` kept after every write,
    files older than ``max_age_h`` hours deleted at startup."""

    keep_files: int = DEFAULT_SAMPLES_KEEP
    max_age_h: float = DEFAULT_SAMPLES_MAX_AGE_H


def _configure_logging(out_dir: str) -> None:
    logger.setLevel(logging.INFO)
    logger.propagate = False  # stdout is the handshake; keep records off any root handler
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    logger.addHandler(stderr_handler)
    log_path = os.path.join(out_dir, persist.LOG_NAME)
    try:
        os.makedirs(out_dir, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError:
        logger.exception("could not open log file %s; logging to stderr only", log_path)


def run_dump(
    store: SnapshotStore,
    stdout: TextIO,
    stdin: Iterable[bytes | str],
    *,
    out_dir: str,
    record_samples: str | None,
    decoder: LineDecoder | None = None,
    retention: SampleRetention | None = None,
    watch_requests: bool = False,
) -> None:
    """Send the handshake, then consume pushed state lines until EOF.

    ``ready`` is written exactly once, before any line is read. ``stdin`` may yield
    ``bytes`` (the real ``sys.stdin.buffer``) or ``str`` (tests); bytes are decoded by
    ``decoder``. Malformed lines are skipped. No argument parsing here, so it is
    directly testable.
    """
    decoder = decoder or LineDecoder()
    retention = retention or SampleRetention()
    protocol.write_line(stdout, "ready")
    watcher = RequestWatcher(out_dir, stdout) if watch_requests else None
    if watcher:
        watcher.start()
    try:
        _consume(store, stdin, decoder, retention, out_dir=out_dir, record_samples=record_samples)
    finally:
        if watcher:
            watcher.stop()
    logger.info("stdin EOF; state dump stopping")


def _consume(
    store: SnapshotStore,
    stdin: Iterable[bytes | str],
    decoder: LineDecoder,
    retention: SampleRetention,
    *,
    out_dir: str,
    record_samples: str | None,
) -> None:
    for raw_line in stdin:
        state = protocol.parse_state_line(decoder.decode(raw_line))
        if state is None:
            continue
        snap = store.record(state)
        persist.write_latest(out_dir, snap)
        if record_samples:
            persist.record_sample(record_samples, snap)
            persist.prune_samples(record_samples, keep_files=retention.keep_files, max_age_h=retention.max_age_h, by_age=False)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sts-state-dump",
        description="Advisor-mode state dumper for sts-game-mcp (one-way; never sends to the game).",
    )
    default_out = os.environ.get("GAME_MCP_STATE_DIR") or persist.default_state_dir()
    parser.add_argument("--out", default=default_out, help="directory for latest.json / latest.meta.json / bridge.log")
    parser.add_argument("--record-samples", metavar="DIR", default=None)
    parser.add_argument("--samples-keep", type=int, default=DEFAULT_SAMPLES_KEEP, metavar="N", help="newest N sample files to keep (default 500)")
    parser.add_argument("--samples-max-age-h", type=float, default=DEFAULT_SAMPLES_MAX_AGE_H, metavar="H", help="delete sample files older than H hours at startup (default 72)")
    parser.add_argument(
        "--stdin-encoding",
        metavar="CODEC",
        default=None,
        help="force the codec used to decode the mod's pushes (default: UTF-8, then the locale code page)",
    )
    args = parser.parse_args(argv)
    if args.stdin_encoding:
        try:
            "".encode(args.stdin_encoding)
        except LookupError:
            parser.error(f"unknown --stdin-encoding {args.stdin_encoding!r}")
    if args.samples_keep < 1 or args.samples_max_age_h <= 0:
        parser.error("--samples-keep must be >= 1 and --samples-max-age-h > 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.out)
    logger.info("sts-state-dump starting; out=%s record_samples=%s", args.out, args.record_samples)

    persist.sweep_temp_files(args.out)
    retention = SampleRetention(keep_files=args.samples_keep, max_age_h=args.samples_max_age_h)
    persist.prune_samples(args.record_samples, keep_files=retention.keep_files, max_age_h=retention.max_age_h, by_age=True)
    last_seq = persist.read_last_seq(args.out)
    if last_seq:
        logger.info("continuing seq from %d found in %s", last_seq, persist.META_NAME)
    store = SnapshotStore(start_seq=last_seq)
    decoder = LineDecoder(args.stdin_encoding)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(newline="\n")  # send a bare LF, not the Windows text-mode CRLF
    logger.info("stdin decoding: %s", args.stdin_encoding or f"utf-8, fallback {decoder.fallback}")
    try:
        run_dump(
            store,
            sys.stdout,
            sys.stdin.buffer,
            out_dir=args.out,
            record_samples=args.record_samples,
            decoder=decoder,
            retention=retention,
            watch_requests=True,
        )
    except KeyboardInterrupt:
        logger.info("interrupted; shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
