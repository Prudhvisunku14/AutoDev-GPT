"""An independent witness between the API filter and OpenCode. It runs INSIDE the check container.

The filter's own log cannot prove what the filter failed to stop. This sits on the wire instead: the
filter's upstream is this port, it writes down the first line of everything that arrives, and it passes
the bytes on to OpenCode untouched. Only the request line is written, so no header or body is kept.

Used only by the filter check. Standard library only, run as `python3 -I`.
"""

from __future__ import annotations

import socket
import socketserver
import threading

LISTEN = ("127.0.0.1", 4097)
TARGET = ("127.0.0.1", 4096)
LOG = "/tmp/witness.log"
MAX_LINE = 2048
_lock = threading.Lock()


def _pump(source: socket.socket, sink: socket.socket) -> None:
    try:
        while chunk := source.recv(65536):
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        with socket.create_connection(TARGET, timeout=60) as upstream:
            first = bytearray()
            while b"\r\n" not in first and len(first) < MAX_LINE:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                first.extend(chunk)
            line = bytes(first).split(b"\r\n", 1)[0][:MAX_LINE].decode("latin-1")
            with _lock, open(LOG, "a", encoding="utf-8") as log:
                log.write(line + "\n")
            upstream.sendall(first)
            back = threading.Thread(target=_pump, args=(upstream, self.request), daemon=True)
            back.start()
            _pump(self.request, upstream)
            back.join(60)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    open(LOG, "a", encoding="utf-8").close()
    with _Server(LISTEN, _Handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
