import struct

TYPE_TEXT = b'T'
TYPE_LIVE = b'L'

HEADER_FMT  = ">cL"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

MAX_PAYLOAD = 8_000_000  

def pack_message(msg_type: bytes, payload: bytes) -> bytes:
    return struct.pack(HEADER_FMT, msg_type, len(payload)) + payload


def send_message(sock, msg_type: bytes, payload: bytes, lock=None):
    packet = pack_message(msg_type, payload)
    if lock is not None:
        with lock:
            sock.sendall(packet)
    else:
        sock.sendall(packet)


def send_text(sock, text: str, lock=None):
    send_message(sock, TYPE_TEXT, text.encode(), lock)


def build_rtsp_request(method: str, cseq: int, session: str = None) -> str:
    lines = [f"{method} rtsp://server/live RTSP/1.0", f"CSeq: {cseq}"]
    if session is not None:
        lines.append(f"Session: {session}")
    return "\r\n".join(lines) + "\r\n\r\n"


def build_rtsp_response(cseq, session: str = None, code: int = 200, reason: str = "OK") -> str:
    lines = [f"RTSP/1.0 {code} {reason}"]
    if cseq is not None:
        lines.append(f"CSeq: {cseq}")
    if session is not None:
        lines.append(f"Session: {session}")
    return "\r\n".join(lines) + "\r\n\r\n"


def parse_rtsp_message(text: str):
    lines = [l for l in text.strip().split("\r\n") if l]
    if not lines:
        return None, {}
    first = lines[0].split()
    if not first:
        return None, {}
    first_token = first[1] if first[0] == "RTSP/1.0" else first[0]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    return first_token, headers



def pack_live_payload(stream_id: str, jpeg_bytes: bytes) -> bytes:
    sid = stream_id.encode()
    if len(sid) > 255:
        raise ValueError("stream_id too long to fit in 1-byte length prefix")
    return struct.pack(">B", len(sid)) + sid + jpeg_bytes


def unpack_live_payload(payload: bytes):
    """Returns (stream_id: str, jpeg_bytes: bytes)."""
    (sid_len,) = struct.unpack(">B", payload[:1])
    sid = payload[1:1 + sid_len].decode(errors="ignore")
    jpeg_bytes = payload[1 + sid_len:]
    return sid, jpeg_bytes


class FrameReceiver:
   
    def __init__(self):
        self._buf = b""

    def feed(self, chunk: bytes):
        self._buf += chunk

    def pop_messages(self):
        messages = []
        while True:
            if len(self._buf) < HEADER_SIZE:
                break
            msg_type, length = struct.unpack(HEADER_FMT, self._buf[:HEADER_SIZE])
            if length > MAX_PAYLOAD or length == 0:
                self._buf = b"" 
                break
            if len(self._buf) < HEADER_SIZE + length:
                break
            payload = self._buf[HEADER_SIZE:HEADER_SIZE + length]
            self._buf = self._buf[HEADER_SIZE + length:]
            messages.append((msg_type, payload))
        return messages