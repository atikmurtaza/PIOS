"""PIOS entrypoint: sensors + episodizer thread + uvicorn. `python -m pios.main`."""
import argparse
import socket
import sys
import threading
import urllib.request

import uvicorn

from . import config, db, memory, sensors
from .api import app

EPISODIZE_EVERY_S = 5 * 60


def _log(msg):
    print(f"[pios] {msg}", file=sys.stderr)


def _already_running(port):
    """True if a PIOS is already answering on this port. Prevents a second
    instance from split-brain writing to the same database."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/status", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def _emit(ev):
    # Sensors call this from their own threads; sqlite connections are
    # thread-bound, so open one per insert (cheap with WAL).
    try:
        con = db.connect()
        try:
            db.insert_event(con, ev)
        finally:
            con.close()
    except Exception as e:
        _log(f"event insert failed: {e!r}")


def _episodizer(stop: threading.Event):
    # own connection: this runs on its own thread
    con = db.connect()
    while not stop.wait(EPISODIZE_EVERY_S):
        try:
            made = memory.episodize(con)
            if made:
                _log(f"episodized: {made} new episode(s)")
            # Distill past days into durable facts (idempotent per day; may load
            # the model briefly once/day, then it unloads via keep_alive).
            facts = memory.consolidate(con)
            if facts:
                _log(f"consolidated: {facts} new fact(s)")
            # Fold the WAL into pios.db so the single file always carries all
            # data (survives reboots/copies even if -wal is left behind).
            db.checkpoint(con)
        except Exception as e:
            _log(f"episodize/consolidate failed: {e!r}")
    con.close()


def main():
    ap = argparse.ArgumentParser(prog="pios")
    ap.add_argument("--port", type=int, default=8321)
    args = ap.parse_args()

    if _already_running(args.port):
        _log(f"PIOS is already running on port {args.port} — not starting a "
             f"second instance (that would split your memory across writers).")
        return

    con = db.connect()  # create schema up front
    db.checkpoint(con)  # fold any WAL left by a prior/unclean shutdown into pios.db
    con.close()
    cfg = config.load()
    started = sensors.start_all(cfg, _emit)
    _log(f"{len(started)} sensor(s) running")

    stop = threading.Event()
    threading.Thread(target=_episodizer, args=(stop,), daemon=True,
                     name="pios-episodizer").start()
    # No model warmup: the LLM loads only when the user chats (RAM discipline).

    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for s in started:
            try:
                s.stop()
            except Exception:
                pass
        # Final checkpoint so a clean stop leaves everything in pios.db.
        try:
            con = db.connect()
            db.checkpoint(con)
            con.close()
        except Exception:
            pass
        _log("stopped")


if __name__ == "__main__":
    main()
