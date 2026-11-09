package com.remotedesk.android.host

import android.content.Context
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.os.Handler
import android.os.HandlerThread
import android.util.DisplayMetrics
import android.view.Display
import com.remotedesk.android.protocol.Msg
import com.remotedesk.android.protocol.SecureChannel
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread
import kotlin.math.max
import kotlin.math.roundToInt

/** One captured screen: pixels plus a hash per 64x64 tile, shared by all viewers. */
class CapturedFrame(val seq: Long, val w: Int, val h: Int, val stride: Int, val pixels: IntArray) {
    val cols = (w + TILE - 1) / TILE
    val rows = (h + TILE - 1) / TILE
    val hashes = LongArray(cols * rows).also { hs ->
        for (r in 0 until rows) for (c in 0 until cols) {
            val tx = c * TILE; val ty = r * TILE
            val tw = minOf(TILE, w - tx); val th = minOf(TILE, h - ty)
            var hash = -3750763034362895579L                   // FNV-1a 64
            for (row in ty until ty + th) {
                var i = row * stride + tx
                val end = i + tw
                while (i < end) { hash = (hash xor pixels[i].toLong()) * 1099511628211L; i++ }
            }
            hs[r * cols + c] = hash
        }
    }

    companion object { const val TILE = 64 }
}

/**
 * Captures the screen once (MediaProjection -> ImageReader) and streams it to any
 * number of viewers, each with its own quality, frame rate and tile state.
 */
class ScreenStreamer(
    private val context: Context,
    private val projection: MediaProjection,
    private val onEnd: () -> Unit,
) {
    private val running = AtomicBoolean(true)
    private val ht = HandlerThread("rd-projection").apply { start() }
    private val handler = Handler(ht.looper)
    private var reader: ImageReader? = null
    private var display: VirtualDisplay? = null
    val viewers = CopyOnWriteArrayList<StreamViewer>()
    private val lock = Object()
    @Volatile var latest: CapturedFrame? = null
        private set
    private var seq = 0L
    @Volatile private var maxSide = 1280
    private var w = 0
    private var h = 0
    private var copyBuf: ByteBuffer? = null
    private var bitmap: Bitmap? = null

    val isRunning get() = running.get()

    fun start() {
        projection.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() = stop()
        }, handler)
        val (cw, ch, d) = captureSize()
        w = cw; h = ch
        val r = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
        reader = r
        display = projection.createVirtualDisplay("RemoteDesk", w, h, d,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, r.surface, null, handler)
        thread(name = "rd-capture", isDaemon = true) { captureLoop() }
    }

    fun addViewer(v: StreamViewer) {
        viewers.add(v)
        v.start(this)
    }

    fun removeViewer(v: StreamViewer) { viewers.remove(v) }

    /** Highest resolution any viewer asked for (quality -> long side). */
    fun qualityChanged() {
        val q = viewers.maxOfOrNull { it.quality } ?: 60
        maxSide = when { q >= 80 -> 1920; q >= 50 -> 1280; else -> 960 }
    }

    /** Viewer threads wait here for a frame newer than [seen]. */
    fun awaitFrame(seen: Long, timeoutMs: Long): CapturedFrame? {
        synchronized(lock) {
            val f = latest
            if (f != null && f.seq != seen) return f
            lock.wait(timeoutMs)
            return latest
        }
    }

    private fun captureSize(): Triple<Int, Int, Int> {
        val dm = context.getSystemService(DisplayManager::class.java)
        val m = DisplayMetrics()
        @Suppress("DEPRECATION")
        dm.getDisplay(Display.DEFAULT_DISPLAY).getRealMetrics(m)
        val k = minOf(1f, maxSide.toFloat() / max(m.widthPixels, m.heightPixels))
        val cw = ((m.widthPixels * k).roundToInt() / 2) * 2
        val ch = ((m.heightPixels * k).roundToInt() / 2) * 2
        return Triple(max(cw, 2), max(ch, 2), m.densityDpi)
    }

    private fun captureLoop() {
        try {
            while (running.get()) {
                val t0 = System.currentTimeMillis()
                checkResize()
                val img = try { reader?.acquireLatestImage() } catch (_: Exception) { null }
                if (img != null) {
                    val frame = try { readFrame(img) } finally { img.close() }
                    if (frame != null) synchronized(lock) { latest = frame; lock.notifyAll() }
                }
                val fps = viewers.maxOfOrNull { it.fps } ?: 10
                val wait = 1000L / fps - (System.currentTimeMillis() - t0)
                if (wait > 0) Thread.sleep(wait)
            }
        } catch (_: Exception) {
        }
        stop()
    }

    private fun checkResize() {
        val (cw, ch, d) = captureSize()
        if (cw == w && ch == h) return
        w = cw; h = ch
        val old = reader
        val r = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
        reader = r
        display?.resize(w, h, d)
        display?.surface = r.surface
        old?.close()
    }

    private fun readFrame(img: android.media.Image): CapturedFrame? {
        if (img.width != w || img.height != h) return null
        val plane = img.planes[0]
        val bw = plane.rowStride / plane.pixelStride
        val need = bw * h * 4
        val buf = copyBuf?.takeIf { it.capacity() == need } ?: ByteBuffer.allocateDirect(need).also { copyBuf = it }
        buf.clear()
        val src = plane.buffer
        src.rewind()
        if (src.remaining() > need) src.limit(src.position() + need)
        buf.put(src)
        buf.rewind()
        val bmp = bitmap?.takeIf { it.width == bw && it.height == h }
            ?: Bitmap.createBitmap(bw, h, Bitmap.Config.ARGB_8888).also { bitmap = it }
        bmp.copyPixelsFromBuffer(buf)
        val pixels = IntArray(bw * h)                    // new array: viewers may still read the old one
        bmp.getPixels(pixels, 0, bw, 0, 0, bw, h)
        return CapturedFrame(++seq, w, h, bw, pixels)
    }

    fun stop() {
        if (!running.getAndSet(false)) return
        viewers.forEach { it.stop() }
        synchronized(lock) { lock.notifyAll() }
        handler.post {
            try { display?.release() } catch (_: Exception) {}
            try { reader?.close() } catch (_: Exception) {}
            try { projection.stop() } catch (_: Exception) {}
            ht.quitSafely()
        }
        onEnd()
    }
}

/** One viewer of the shared screen. [onMessage] gets input / chat / clipboard messages. */
class StreamViewer(
    val channel: SecureChannel,
    val peer: String,
    val mayControl: Boolean,
    private val onMessage: (StreamViewer, Int, ByteArray) -> Unit,
    private val onClosed: (StreamViewer) -> Unit,
) {
    @Volatile var name: String = peer
    @Volatile var control = false
    @Volatile var quality = 60
    @Volatile var fps = 10
    val since = System.currentTimeMillis()
    private val running = AtomicBoolean(true)
    @Volatile private var forceFull = true
    private lateinit var streamer: ScreenStreamer

    fun start(s: ScreenStreamer) {
        streamer = s
        thread(name = "rd-view-send", isDaemon = true) { sendLoop() }
        thread(name = "rd-view-recv", isDaemon = true) { recvLoop() }
    }

    fun send(type: Int, payload: ByteArray = ByteArray(0)) {
        try { channel.send(type, payload) } catch (_: Exception) { stop() }
    }

    private fun sendLoop() {
        var seen = -1L
        var infoW = -1
        var infoH = -1
        var sent = LongArray(0)
        val out = ByteArrayOutputStream(16 * 1024)
        try {
            while (running.get()) {
                val f = streamer.awaitFrame(seen, 1000) ?: continue
                if (f.seq == seen && !forceFull) continue
                val t0 = System.currentTimeMillis()
                seen = f.seq
                if (f.w != infoW || f.h != infoH) {
                    infoW = f.w; infoH = f.h
                    channel.send(Msg.FRAME_INFO, ("{\"w\": ${f.w}, \"h\": ${f.h}, \"monitors\": " +
                            "[{\"w\": ${f.w}, \"h\": ${f.h}}], \"monitor\": 0}").toByteArray())
                    sent = LongArray(0)
                }
                if (forceFull || sent.size != f.hashes.size) {
                    forceFull = false
                    sent = LongArray(f.hashes.size) { it.toLong() xor 0x5bd1e995L }   // never equal to a hash
                }
                var count = 0
                for (r in 0 until f.rows) for (c in 0 until f.cols) {
                    val i = r * f.cols + c
                    if (sent[i] == f.hashes[i]) continue
                    sent[i] = f.hashes[i]
                    val tx = c * CapturedFrame.TILE; val ty = r * CapturedFrame.TILE
                    val tw = minOf(CapturedFrame.TILE, f.w - tx); val th = minOf(CapturedFrame.TILE, f.h - ty)
                    val tile = Bitmap.createBitmap(f.pixels, ty * f.stride + tx, f.stride, tw, th, Bitmap.Config.ARGB_8888)
                    out.reset()
                    out.write(byteArrayOf((tx shr 8).toByte(), tx.toByte(), (ty shr 8).toByte(), ty.toByte()))
                    tile.compress(Bitmap.CompressFormat.JPEG, quality, out)
                    tile.recycle()
                    channel.send(Msg.TILE, out.toByteArray())
                    count++
                }
                if (count > 0) channel.send(Msg.FRAME_END)
                val wait = 1000L / fps - (System.currentTimeMillis() - t0)
                if (wait > 0) Thread.sleep(wait)
            }
        } catch (_: Exception) {
        }
        stop()
    }

    private fun recvLoop() {
        try {
            while (running.get()) {
                val (type, data) = channel.recv()
                when (type) {
                    Msg.BYE -> break
                    Msg.SET_QUALITY -> {
                        val o = JSONObject(String(data))
                        quality = o.optInt("quality", quality).coerceIn(10, 95)
                        fps = o.optInt("fps", fps).coerceIn(1, 30)
                        forceFull = true
                        streamer.qualityChanged()
                    }
                    Msg.SET_MONITOR, Msg.KEYFRAME -> forceFull = true
                    else -> onMessage(this, type, data)
                }
            }
        } catch (_: Exception) {
        }
        stop()
    }

    fun stop() {
        if (!running.getAndSet(false)) return
        thread(isDaemon = true) { channel.close() }
        if (::streamer.isInitialized) streamer.removeViewer(this)
        onClosed(this)
    }
}
