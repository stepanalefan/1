package com.remotedesk.android.protocol

// Pure Kotlin/JVM (no Android APIs) so it can be tested against the Python host on a PC.
// Must stay byte-compatible with remote_desk/protocol.py.

import org.bouncycastle.math.ec.rfc7748.X25519
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

object Msg {
    const val FRAME_INFO = 1
    const val TILE = 2
    const val FRAME_END = 3
    const val MOUSE = 10
    const val KEY = 11
    const val SET_MONITOR = 12
    const val SET_QUALITY = 13
    const val CLIPBOARD = 20
    const val CHAT = 21
    const val BYE = 99
}

class AuthException(message: String) : IOException(message)

/** Minimal JSON writer for the small objects the viewer sends. */
object Json {
    fun obj(vararg kv: Pair<String, Any?>): ByteArray = buildString {
        append('{')
        kv.forEachIndexed { i, (k, v) ->
            if (i > 0) append(", ")
            append(quote(k)).append(": ").append(value(v))
        }
        append('}')
    }.toByteArray(Charsets.UTF_8)

    private fun value(v: Any?): String = when (v) {
        null -> "null"
        is Boolean -> v.toString()
        is Int, is Long -> v.toString()
        is Float -> if (v.isFinite()) v.toString() else "0"
        is Double -> if (v.isFinite()) v.toString() else "0"
        else -> quote(v.toString())
    }

    fun quote(s: String): String = buildString {
        append('"')
        for (c in s) when {
            c == '"' -> append("\\\"")
            c == '\\' -> append("\\\\")
            c == '\n' -> append("\\n")
            c == '\r' -> append("\\r")
            c == '\t' -> append("\\t")
            c < ' ' -> append(String.format("\\u%04x", c.code))
            else -> append(c)
        }
        append('"')
    }
}

internal object Crypto {
    private fun hmac(key: ByteArray, vararg parts: ByteArray): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        parts.forEach { mac.update(it) }
        return mac.doFinal()
    }

    fun sha256(data: ByteArray): ByteArray = MessageDigest.getInstance("SHA-256").digest(data)

    /** PBKDF2-HMAC-SHA256 with a 32-byte output (one block). */
    fun pbkdf2(password: ByteArray, salt: ByteArray, iterations: Int): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        // HMAC keys of zero length are rejected by SecretKeySpec; HMAC pads short keys with zeros,
        // so a single zero byte is equivalent to an empty password.
        mac.init(SecretKeySpec(if (password.isEmpty()) ByteArray(1) else password, "HmacSHA256"))
        mac.update(salt)
        mac.update(byteArrayOf(0, 0, 0, 1))
        var u = mac.doFinal()
        val out = u.copyOf()
        for (i in 1 until iterations) {
            u = mac.doFinal(u)
            for (j in out.indices) out[j] = (out[j].toInt() xor u[j].toInt()).toByte()
        }
        return out
    }

    fun hkdf(ikm: ByteArray, salt: ByteArray, info: ByteArray, length: Int): ByteArray {
        val prk = hmac(salt, ikm)
        val out = ByteArray(length)
        var t = ByteArray(0)
        var pos = 0
        var counter = 1
        while (pos < length) {
            t = hmac(prk, t, info, byteArrayOf(counter.toByte()))
            val n = minOf(t.size, length - pos)
            System.arraycopy(t, 0, out, pos, n)
            pos += n
            counter++
        }
        return out
    }

    fun proof(pwKey: ByteArray, role: String, transcript: ByteArray, shared: ByteArray) =
        hmac(pwKey, role.toByteArray(Charsets.US_ASCII), transcript, shared)
}

/** Encrypted, framed message channel (AES-256-GCM, counter nonces). */
class SecureChannel internal constructor(
    private val socket: Socket, sendKey: ByteArray, recvKey: ByteArray,
) {
    private val out = DataOutputStream(BufferedOutputStream(socket.getOutputStream(), 65536))
    private val inp = DataInputStream(BufferedInputStream(socket.getInputStream(), 65536))
    private val txKey = SecretKeySpec(sendKey, "AES")
    private val rxKey = SecretKeySpec(recvKey, "AES")
    private val txCipher = Cipher.getInstance("AES/GCM/NoPadding")
    private val rxCipher = Cipher.getInstance("AES/GCM/NoPadding")
    private var txN = 0L
    private var rxN = 0L
    @Volatile var closed = false
        private set

    private fun nonce(n: Long) = ByteBuffer.allocate(12).putInt(0).putLong(n).array()

    @Synchronized
    fun send(type: Int, payload: ByteArray = ByteArray(0)) {
        txCipher.init(Cipher.ENCRYPT_MODE, txKey, GCMParameterSpec(128, nonce(txN++)))
        txCipher.update(byteArrayOf(type.toByte()))
        val ct = txCipher.doFinal(payload)
        out.writeInt(ct.size)
        out.write(ct)
        out.flush()
    }

    fun send(type: Int, text: String) = send(type, text.toByteArray(Charsets.UTF_8))

    /** Blocks until the next message; returns (type, payload). */
    fun recv(): Pair<Int, ByteArray> {
        val n = inp.readInt()
        if (n < 17 || n > 64 * 1024 * 1024) throw IOException("bad message length $n")
        val ct = ByteArray(n)
        inp.readFully(ct)
        rxCipher.init(Cipher.DECRYPT_MODE, rxKey, GCMParameterSpec(128, nonce(rxN++)))
        val pt = rxCipher.doFinal(ct)
        return (pt[0].toInt() and 0xff) to pt.copyOfRange(1, pt.size)
    }

    fun close() {
        if (closed) return
        closed = true
        try { send(Msg.BYE) } catch (_: Exception) {}
        try { socket.close() } catch (_: Exception) {}
    }
}

object Handshake {
    private val MAGIC = "RDSK1\u0000".toByteArray(Charsets.US_ASCII)
    private const val PBKDF2_ITERATIONS = 200_000

    fun viewer(socket: Socket, password: String): SecureChannel {
        val inp = DataInputStream(socket.getInputStream())
        val out = socket.getOutputStream()
        val head = ByteArray(MAGIC.size + 64)
        inp.readFully(head)
        if (!head.copyOfRange(0, MAGIC.size).contentEquals(MAGIC))
            throw IOException("Not a RemoteDesk desk")
        val hpub = head.copyOfRange(MAGIC.size, MAGIC.size + 32)
        val salt = head.copyOfRange(MAGIC.size + 48, MAGIC.size + 64)

        val rnd = SecureRandom()
        val priv = ByteArray(X25519.SCALAR_SIZE).also { X25519.generatePrivateKey(rnd, it) }
        val pub = ByteArray(X25519.POINT_SIZE).also { X25519.generatePublicKey(priv, 0, it, 0) }
        val shared = ByteArray(X25519.POINT_SIZE)
        if (!X25519.calculateAgreement(priv, 0, hpub, 0, shared, 0))
            throw IOException("Invalid key from desk")
        val nonce = ByteArray(16).also { rnd.nextBytes(it) }

        val transcript = head + pub + nonce
        val pwKey = Crypto.pbkdf2(password.toByteArray(Charsets.UTF_8), salt, PBKDF2_ITERATIONS)
        val okm = Crypto.hkdf(shared, Crypto.sha256(transcript),
            "remotedesk keys".toByteArray(Charsets.US_ASCII), 64)
        out.write(pub + nonce + Crypto.proof(pwKey, "viewer", transcript, shared))
        out.flush()

        val hproof = ByteArray(32)
        try {
            inp.readFully(hproof)
        } catch (e: EOFException) {
            throw IOException("The desk closed the connection (too many wrong passwords?)")
        }
        if (!MessageDigest.isEqual(hproof, Crypto.proof(pwKey, "host", transcript, shared)))
            throw AuthException("Wrong password")
        // host->viewer key first, viewer->host second (see protocol.py)
        return SecureChannel(socket, okm.copyOfRange(32, 64), okm.copyOfRange(0, 32))
    }
}

object Connector {
    fun isDeskId(target: String): Boolean {
        val t = target.replace(" ", "")
        return t.length == 9 && t.all { it.isDigit() }
    }

    /** "host", "host:port" or "[v6]:port" -> (host, port). */
    fun parseHostPort(s: String, defaultPort: Int): Pair<String, Int>? {
        val t = s.trim()
        if (t.isEmpty()) return null
        if (t.startsWith("[")) {
            val end = t.indexOf(']')
            if (end < 0) return null
            val port = t.substring(end + 1).removePrefix(":").toIntOrNull() ?: defaultPort
            return t.substring(1, end) to port
        }
        val colon = t.lastIndexOf(':')
        if (colon > 0 && t.indexOf(':') == colon) {
            return t.substring(0, colon) to (t.substring(colon + 1).toIntOrNull() ?: defaultPort)
        }
        return t to defaultPort
    }

    /**
     * Connects [socket] to a desk: a 9-digit ID goes through the relay [server],
     * anything else is treated as a direct address (default port 7071).
     * Close the socket from another thread to cancel.
     */
    fun open(socket: Socket, target: String, password: String, server: Pair<String, Int>?): SecureChannel {
        val t = target.replace(" ", "")
        if (isDeskId(t)) {
            server ?: throw IOException("No server configured. Add your RemoteDesk Server in Settings.")
            socket.connect(InetSocketAddress(server.first, server.second), 15_000)
            socket.soTimeout = 30_000
            socket.getOutputStream().apply {
                write("{\"role\": \"viewer\", \"id\": ${Json.quote(t)}}\n".toByteArray()); flush()
            }
            val reply = readLine(socket.getInputStream())
            if (!Regex("\"ok\"\\s*:\\s*true").containsMatchIn(reply)) {
                val err = Regex("\"error\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"").find(reply)?.groupValues?.get(1)
                throw IOException(err ?: "The server refused the connection")
            }
        } else {
            val (host, port) = parseHostPort(t, 7071) ?: throw IOException("Invalid address")
            socket.connect(InetSocketAddress(host, port), 15_000)
        }
        socket.tcpNoDelay = true
        socket.soTimeout = 120_000          // the remote user may take a while to accept
        val ch = Handshake.viewer(socket, password)
        socket.soTimeout = 0
        return ch
    }

    private fun readLine(inp: InputStream): String {
        val buf = java.io.ByteArrayOutputStream()
        while (true) {
            val c = inp.read()
            if (c < 0) throw IOException("Server closed the connection")
            if (c == '\n'.code) break
            buf.write(c)
            if (buf.size() > 65536) throw IOException("Bad reply from server")
        }
        return buf.toString("UTF-8")
    }
}
