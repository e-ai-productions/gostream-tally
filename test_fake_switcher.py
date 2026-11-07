#!/usr/bin/env python3
"""Fake switcher to test the bridge end to end: imitates the GSP protocol."""
import json
import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gostream_tally import build_packet, parse_packets  # noqa: E402

STATE = {"pgmIndex": 3, "pvwIndex": 5, "keyOnAir": 0, "dskOnAir": 0, "ftb": 0, "autoTransition": 0}


def handle(conn: socket.socket) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] client CONNECTED", flush=True)
    buf = bytearray()
    conn.settimeout(1.0)
    started = time.time()
    pushed = False
    try:
        while True:
            if not pushed and time.time() - started > PUSH_DELAY:
                # simuleer een druk op de knop: camera 4 gaat on air
                STATE["pgmIndex"] = 4
                STATE["pvwIndex"] = 3
                conn.sendall(build_packet({"id": "pgmIndex", "type": "pus", "value": [4]}))
                conn.sendall(build_packet({"id": "pvwIndex", "type": "pus", "value": [3]}))
                pushed = True
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                return
            buf.extend(chunk)
            for cmd in parse_packets(buf):
                pid = cmd.get("id")
                if cmd.get("type") == "get" and pid in STATE:
                    conn.sendall(build_packet({"id": pid, "type": "get", "value": [STATE[pid]]}))
    finally:
        print(f"[{time.strftime('%H:%M:%S')}] client GONE", flush=True)
        conn.close()


PUSH_DELAY = float(sys.argv[2]) if len(sys.argv) > 2 else 1.5


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19099
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    print(f"fake switcher listening on 127.0.0.1:{port}", flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
