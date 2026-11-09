package com.remotedesk.android

import com.remotedesk.android.protocol.SecureChannel
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * Holds the open connection while SessionActivity runs. All sends go through a
 * single background thread (network on the main thread is not allowed) and keep
 * their order.
 */
object Session {
    @Volatile var channel: SecureChannel? = null
    @Volatile var title: String = ""
    /** Chat history of the current session (kept while the screen rotates etc.). */
    val chat = java.util.Collections.synchronizedList(mutableListOf<com.remotedesk.android.protocol.ChatMessage>())
    private val sender = Executors.newSingleThreadExecutor { r -> Thread(r, "rd-send").apply { isDaemon = true } }

    fun send(type: Int, payload: ByteArray = ByteArray(0)) {
        val ch = channel ?: return
        sender.execute { try { ch.send(type, payload) } catch (_: Exception) {} }
    }

    // Pointer moves are coalesced: only the newest position is sent.
    private val pendingMove = AtomicReference<ByteArray?>(null)
    private val moveQueued = AtomicBoolean(false)

    fun sendMove(payload: ByteArray, type: Int) {
        pendingMove.set(payload)
        if (moveQueued.compareAndSet(false, true)) {
            val ch = channel ?: return
            sender.execute {
                moveQueued.set(false)
                val p = pendingMove.getAndSet(null) ?: return@execute
                try { ch.send(type, p) } catch (_: Exception) {}
            }
        }
    }

    fun close() {
        val ch = channel ?: return
        channel = null
        chat.clear()
        sender.execute { ch.close() }
    }
}
