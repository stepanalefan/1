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
import com.remotedesk.android.protocol.Json
import com.remotedesk.android.protocol.Msg
import com.remotedesk.android.protocol.SecureChannel
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * Streams the screen to one viewer: MediaProjection -> ImageReader -> changed
 * 64x64 tiles as JPEG (same format as the PC host). Messages that are not about
 * the stream (mouse, keys, clipboard, chat) are passed to [onMessage].
 */
class ScreenStreamer(
    private val context: Context,
    private val projection: MediaProjection,
    private val channel: SecureChannel,
    private val onMessage: (Int, ByteArray) -> Unit,
    private val onEnd: () -> Unit,
) {
    private val running = AtomicBoolean(true)
    private val ht = HandlerThread("rd-projection").apply { start() }
    private val handler = Handler(ht.looper)
    private var reader: ImageReader? = null
    private var display: VirtualDisplay? = null
    @Volatile private var quality = 60
    @Volatile private var fps = 10
    @Volatile private var maxSide = 1280
    @Volatile private var forceFull = true

    private var w = 0
    private var h = 0
    private var dpi = 0
    private val hashes = HashMap<Long, Long>()
    private var pixels = IntArray(0)
    private var frameBitmap: Bitmap? = null
    private var copyBuf: ByteBuffer? = null
    private var haveFrame = false

    val isRunning get() = running.get()

    fun start() {
        projection.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() = stop()
        }, handler)
        val (cw, ch, d) = captureSize()
        w = cw; h = ch; dpi = d
        val r = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
        reader = r
        display = projection.createVirtualDisplay("RemoteDesk", w, h, dpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, r.surface, null, handler)
        thread(name = "rd-stream", isDaemon = true) { sendLoop() }
        thread(name = "rd-session-recv", isDaemon = true) { recvLoop() }
    }

    /** Capture size: the real screen, scaled so the long side is at most [maxSide]. */
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

    private fun sendInfo() {
        channel.send(Msg.FRAME_INFO, ("{\"w\": $w, \"h\": $h, \"monitors\": [{\"w\": $w, \"h\": $h}], " +
                "\"monitor\": 0}").toByteArray())
    }

    private fun sendLoop() {
        try {
            sendInfo()
            while (running.get()) {
                val t0 = System.currentTimeMillis()
                checkResize()
                val img = try { reader?.acquireLatestImage() } catch (_: Exception) { null }
                if (img != null) {
                    try { readFrame(img) } finally { img.close() }
                    sendTiles()
                } else if (forceFull && haveFrame) {
                    sendTiles()
                }
                val wait = 1000L / fps - (System.currentTimeMillis() - t0)
                if (wait > 0) Thread.sleep(wait)
            }
        } catch (_: Exception) {
        }
        stop()
    }

    /** Rotation / quality change: rebuild the reader at the new size. */
    private fun checkResize() {
        val (cw, ch, d) = captureSize()
        if (cw == w && ch == h) return
        w = cw; h = ch; dpi = d
        val old = reader
        val r = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
        reader = r
        display?.resize(w, h, dpi)
        display?.surface = r.surface
        old?.close()
        haveFrame = false
        hashes.clear()
        sendInfo()
    }

    private fun readFrame(img: android.media.Image) {
        if (img.width != w || img.height != h) return
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
        val bmp = frameBitmap?.takeIf { it.width == bw && it.height == h }
            ?: Bitmap.createBitmap(bw, h, Bitmap.Config.ARGB_8888).also { frameBitmap = it }
        bmp.copyPixelsFromBuffer(buf)
        if (pixels.size != bw * h) pixels = IntArray(bw * h)
        bmp.getPixels(pixels, 0, bw, 0, 0, bw, h)
        stride = bw
        haveFrame = true
    }

    private var stride = 0

    private fun sendTiles() {
        if (!haveFrame) return
        if (forceFull) { hashes.clear(); forceFull = false }
        var sent = 0
        val out = ByteArrayOutputStream(16 * 1024)
        var ty = 0
        while (ty < h) {
            val th = minOf(TILE, h - ty)
            var tx = 0
            while (tx < w) {
                val tw = minOf(TILE, w - tx)
                var hash = -3750763034362895579L          // FNV-1a 64
                for (row in ty until ty + th) {
                    var i = row * stride + tx
                    val end = i + tw
                    while (i < end) {
                        hash = (hash xor pixels[i].toLong()) * 1099511628211L
                        i++
                    }
                }
                val key = (tx.toLong() shl 32) or ty.toLong()
                if (hashes[key] != hash) {
                    hashes[key] = hash
                    val tile = Bitmap.createBitmap(pixels, ty * stride + tx, stride, tw, th, Bitmap.Config.ARGB_8888)
                    out.reset()
                    out.write(byteArrayOf((tx shr 8).toByte(), tx.toByte(), (ty shr 8).toByte(), ty.toByte()))
                    tile.compress(Bitmap.CompressFormat.JPEG, quality, out)
                    tile.recycle()
                    channel.send(Msg.TILE, out.toByteArray())
                    sent++
                }
                tx += TILE
            }
            ty += TILE
        }
        if (sent > 0) channel.send(Msg.FRAME_END)
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
                        // phones: fewer pixels on "fast", full HD-ish on "best"
                        maxSide = when { quality >= 80 -> 1920; quality >= 50 -> 1280; else -> 960 }
                        forceFull = true
                    }
                    Msg.SET_MONITOR -> forceFull = true
                    else -> onMessage(type, data)
                }
            }
        } catch (_: Exception) {
        }
        stop()
    }

    fun stop() {
        if (!running.getAndSet(false)) return
        thread(isDaemon = true) { channel.close() }
        handler.post {
            try { display?.release() } catch (_: Exception) {}
            try { reader?.close() } catch (_: Exception) {}
            try { projection.stop() } catch (_: Exception) {}
            ht.quitSafely()
        }
        onEnd()
    }

    companion object {
        private const val TILE = 64
    }
}
