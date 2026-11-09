"""Wire protocol: framing, authenticated key exchange, encrypted messages.

Handshake (runs end-to-end, the relay only forwards bytes):
  host   -> viewer : MAGIC | host_pub(32) | host_nonce(16) | pw_salt(16)
  viewer -> host   : viewer_pub(32) | viewer_nonce(16) | proof_v(32)
  host   -> viewer : proof_h(32)
  shared  = X25519(host, viewer)
  pw_key  = PBKDF2-SHA256(password, pw_salt, 200k)
  keys    = HKDF(shared, salt=nonces, info=transcript)
  proof_x = HMAC(pw_key, role | transcript | shared)
A relay that swaps keys cannot produce valid proofs without the password.
After the handshake every message is AES-256-GCM with per-direction keys
and a counter nonce (replays / reordering are rejected).
"""
import hashlib, hmac, json, os, socket, struct, threading

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization

MAGIC = b"RDSK1\0"
MAX_MSG = 64 * 1024 * 1024

# message types
FRAME_INFO = 1   # json {w,h,monitors,monitor}
TILE = 2         # x,y (u16,u16) + jpeg
FRAME_END = 3
MOUSE = 10       # json {x,y (0..1), btn, action, dx, dy}
KEY = 11         # json {key, text, down}
SET_MONITOR = 12 # json {index}
SET_QUALITY = 13 # json {quality, fps}
CLIPBOARD = 20   # utf-8 text
CHAT = 21        # json {from, text, ts}  (older clients: plain utf-8 text)
FILE_BEGIN = 30  # json {name,size}
FILE_DATA = 31
FILE_END = 32
CURSOR = 40      # json {x,y}
ROLE = 41        # host -> viewer: json {control, controller, viewers: [{name, control}]}
HELLO = 42       # viewer -> host: json {name}
BYE = 99


class AuthError(Exception):
    pass


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return bytes(buf)


def send_line(sock, obj):
    sock.sendall(json.dumps(obj).encode() + b"\n")


def recv_line(sock, limit=65536):
    buf = bytearray()
    while not buf.endswith(b"\n"):
        c = sock.recv(1)
        if not c:
            raise ConnectionError("connection closed")
        buf += c
        if len(buf) > limit:
            raise ConnectionError("line too long")
    return json.loads(buf)


def _raw(pub):
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _derive(shared, transcript, password, salt):
    pw_key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    okm = HKDF(hashes.SHA256(), 64, salt=hashlib.sha256(transcript).digest(),
               info=b"remotedesk keys").derive(shared)
    def proof(role):
        return hmac.new(pw_key, role + transcript + shared, hashlib.sha256).digest()
    return okm[:32], okm[32:], proof


class SecureChannel:
    """Thread-safe encrypted message channel over a connected socket."""

    def __init__(self, sock, send_key, recv_key):
        self.sock = sock
        self._tx, self._rx = AESGCM(send_key), AESGCM(recv_key)
        self._tx_n = self._rx_n = 0
        self._lock = threading.Lock()
        self.closed = False

    def send(self, mtype, payload=b""):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        elif isinstance(payload, str):
            payload = payload.encode()
        with self._lock:
            nonce = struct.pack(">4xQ", self._tx_n)
            self._tx_n += 1
            ct = self._tx.encrypt(nonce, bytes([mtype]) + payload, None)
            self.sock.sendall(struct.pack(">I", len(ct)) + ct)

    def recv(self):
        (n,) = struct.unpack(">I", recv_exact(self.sock, 4))
        if n > MAX_MSG:
            raise ConnectionError("message too large")
        ct = recv_exact(self.sock, n)
        nonce = struct.pack(">4xQ", self._rx_n)
        self._rx_n += 1
        pt = self._rx.decrypt(nonce, ct, None)  # raises InvalidTag on tamper
        return pt[0], pt[1:]

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.send(BYE)
        except Exception:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        self.sock.close()


def host_handshake(sock, password):
    priv = X25519PrivateKey.generate()
    pub, nonce, salt = _raw(priv.public_key()), os.urandom(16), os.urandom(16)
    sock.sendall(MAGIC + pub + nonce + salt)
    data = recv_exact(sock, 32 + 16 + 32)
    vpub, vnonce, vproof = data[:32], data[32:48], data[48:]
    shared = priv.exchange(X25519PublicKey.from_public_bytes(vpub))
    transcript = MAGIC + pub + nonce + salt + vpub + vnonce
    k_h2v, k_v2h, proof = _derive(shared, transcript, password, salt)
    if not hmac.compare_digest(vproof, proof(b"viewer")):
        sock.sendall(b"\0" * 32)
        raise AuthError("wrong password")
    sock.sendall(proof(b"host"))
    return SecureChannel(sock, k_h2v, k_v2h)


def viewer_handshake(sock, password):
    head = recv_exact(sock, len(MAGIC) + 64)
    if not head.startswith(MAGIC):
        raise ConnectionError("not a RemoteDesk host")
    body = head[len(MAGIC):]
    hpub, hnonce, salt = body[:32], body[32:48], body[48:64]
    priv = X25519PrivateKey.generate()
    pub, nonce = _raw(priv.public_key()), os.urandom(16)
    shared = priv.exchange(X25519PublicKey.from_public_bytes(hpub))
    transcript = MAGIC + hpub + hnonce + salt + pub + nonce
    k_h2v, k_v2h, proof = _derive(shared, transcript, password, salt)
    sock.sendall(pub + nonce + proof(b"viewer"))
    hproof = recv_exact(sock, 32)
    if not hmac.compare_digest(hproof, proof(b"host")):
        raise AuthError("wrong password or host could not be verified")
    return SecureChannel(sock, k_v2h, k_h2v)
