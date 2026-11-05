package com.remotedesk.android.protocol

// Pure Kotlin/JVM: the network side of sharing this device (same behaviour as
// remote_desk/host.py). Screen capture and input live in the Android layer.

import java.io.IOException
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap
import kotlin.concurrent.thread

class DeskHost(
    @Volatile var password: String,
    private val listener: Listener,
    private val directPort: Int = 7071,
    private val server: Pair<String, Int>? = null,
    @Volatile var deskId: String? = null,
) {
    interface Listener {
        fun onId(id: String) {}
        fun onStatus(text: String, online: Boolean) {}
        /** Password was correct. Return quickly; take ownership of [channel] or close it. */
        fun onIncoming(channel: SecureChannel, peer: String)
        fun onRejected(peer: String) {}
    }

    @Volatile private var running = false
    private var serverSocket: ServerSocket? = null
    @Volatile private var control: Socket? = null
    private val fails = ConcurrentHashMap<String, Pair<Int, Long>>()

    fun start() {
        running = true
        if (directPort > 0) thread(name = "rd-listen", isDaemon = true) { listenLoop() }
        if (server != null) thread(name = "rd-relay", isDaemon = true) { relayLoop(server) }
    }

    fun stop() {
        running = false
        try { serverSocket?.close() } catch (_: Exception) {}
        try { control?.close() } catch (_: Exception) {}
    }

    private fun listenLoop() {
        val ss = try {
            ServerSocket().apply { reuseAddress = true; bind(InetSocketAddress(directPort)) }
        } catch (e: IOException) {
            listener.onStatus("Direct port $directPort unavailable: ${e.message}", false)
            return
        }
        serverSocket = ss
        while (running) {
            val s = try { ss.accept() } catch (_: IOException) { break }
            val ip = s.inetAddress.hostAddress?.substringBefore('%')?.removePrefix("::ffff:") ?: "?"
            thread(name = "rd-in", isDaemon = true) { incoming(s, ip) }
        }
    }

    private fun relayLoop(srv: Pair<String, Int>) {
        while (running) {
            try {
                val ctrl = Socket()
                control = ctrl
                ctrl.connect(InetSocketAddress(srv.first, srv.second), 10_000)
                ctrl.soTimeout = 15_000
                Connector.sendLine(ctrl, "{\"role\": \"host\", \"id\": ${deskId?.let { Json.quote(it) } ?: "null"}}")
                val reply = Connector.readLine(ctrl.getInputStream())
                val id = Regex("\"id\"\\s*:\\s*\"(\\d+)\"").find(reply)?.groupValues?.get(1)
                    ?: throw IOException("Bad reply from server")
                deskId = id
                listener.onId(id)
                listener.onStatus("Online — server ${srv.first}:${srv.second}", true)
                ctrl.soTimeout = 0
                val pinger = thread(name = "rd-ping", isDaemon = true) {
                    try {
                        while (running && !ctrl.isClosed) {
                            Thread.sleep(20_000)
                            ctrl.getOutputStream().apply { write('.'.code); flush() }
                        }
                    } catch (_: Exception) {}
                }
                val inp = ctrl.getInputStream()
                while (running) {
                    val line = Connector.readLine(inp)
                    val token = Regex("\"connect\"\\s*:\\s*\"([0-9a-f]+)\"").find(line)?.groupValues?.get(1) ?: continue
                    val from = Regex("\"from\"\\s*:\\s*\"([^\"]*)\"").find(line)?.groupValues?.get(1) ?: "unknown"
                    thread(name = "rd-data", isDaemon = true) {
                        try {
                            val d = Socket()
                            d.connect(InetSocketAddress(srv.first, srv.second), 10_000)
                            Connector.sendLine(d, "{\"role\": \"host-data\", \"token\": ${Json.quote(token)}}")
                            incoming(d, "$from (via server)")
                        } catch (_: Exception) {}
                    }
                }
                pinger.interrupt()
            } catch (e: Exception) {
                if (!running) return
                listener.onStatus("Server ${srv.first}:${srv.second} unreachable (${e.message}); retrying…", false)
                try { Thread.sleep(5_000) } catch (_: InterruptedException) { return }
            } finally {
                try { control?.close() } catch (_: Exception) {}
            }
        }
    }

    private fun incoming(s: Socket, peer: String) {
        val key = peer.substringBefore(" (")
        val (n, last) = fails[key] ?: (0 to 0L)
        if (n >= 5 && System.currentTimeMillis() - last < 60_000L * minOf(n - 4, 30)) {
            try { s.close() } catch (_: Exception) {}
            return
        }
        try {
            s.tcpNoDelay = true
            s.soTimeout = 30_000
            val ch = Handshake.host(s, password)
            s.soTimeout = 0
            fails.remove(key)
            listener.onIncoming(ch, peer)
        } catch (e: AuthException) {
            fails[key] = (n + 1) to System.currentTimeMillis()
            listener.onRejected(peer)
            try { s.close() } catch (_: Exception) {}
        } catch (_: Exception) {
            try { s.close() } catch (_: Exception) {}
        }
    }
}
