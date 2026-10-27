"""
Wire protocol shared by the agent, the collector and the tools.

TCP is a stream of bytes with no message boundaries, so every message is framed:

    +----------------+-----------------------------+
    | 4 bytes: length | JSON message (UTF-8 text)   |
    +----------------+-----------------------------+

The length is an unsigned 32-bit number in network byte order (big endian).
The receiver first reads exactly 4 bytes, then reads exactly `length` bytes.
"""
import json
import socket
import struct

MAX_FRAME = 5 * 1024 * 1024        # refuse messages larger than 5 MB
_HEADER = struct.Struct("!I")


def send_msg(sock, obj):
    """Encode `obj` as JSON and send it as one framed message."""
    data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    sock.sendall(_HEADER.pack(len(data)) + data)


def recv_exact(sock, n):
    """Read exactly n bytes (a single recv() may return fewer). Raises ConnectionError if the peer closed."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed by peer")
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(sock):
    """Read one framed message and return it as a Python object.
    Raises ConnectionError (peer closed), socket.timeout (nothing arrived in time),
    or ValueError (frame too large or not valid JSON)."""
    (length,) = _HEADER.unpack(recv_exact(sock, _HEADER.size))
    if length > MAX_FRAME:
        raise ValueError(f"frame too large ({length} bytes)")
    return json.loads(recv_exact(sock, length).decode("utf-8"))


def parse_hostport(text, default_port=9000):
    """'10.0.0.5:9000' -> ('10.0.0.5', 9000); '10.0.0.5' -> ('10.0.0.5', default_port)"""
    if ":" in text:
        host, port = text.rsplit(":", 1)
        return host, int(port)
    return text, default_port
