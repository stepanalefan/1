package com.remotedesk.android

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.view.Surface
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * H.264 (Annex B) -> hardware decoder -> [surface]. Frames are dropped rather than
 * queued when the decoder is busy; after a drop it waits for a keyframe and asks
 * the desk for one via [onNeedKeyframe].
 */
class VideoDecoder(private val surface: Surface, private val onNeedKeyframe: () -> Unit) {
    private val ht = HandlerThread("rd-video").apply { start() }
    private val inputs = LinkedBlockingQueue<Int>()
    @Volatile private var codec: MediaCodec? = null
    private var width = 0
    private var height = 0
    @Volatile private var needKey = true

    @Synchronized
    fun configure(w: Int, h: Int) {
        val ew = w - w % 2
        val eh = h - h % 2
        if (codec != null && ew == width && eh == height) return
        releaseCodec()
        width = ew; height = eh
        val fmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, ew, eh)
        if (Build.VERSION.SDK_INT >= 30) fmt.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
        val c = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        inputs.clear()
        c.setCallback(object : MediaCodec.Callback() {
            override fun onInputBufferAvailable(mc: MediaCodec, index: Int) { if (mc === codec) inputs.offer(index) }
            override fun onOutputBufferAvailable(mc: MediaCodec, index: Int, info: MediaCodec.BufferInfo) {
                try { mc.releaseOutputBuffer(index, true) } catch (_: Exception) {}
            }
            override fun onError(mc: MediaCodec, e: MediaCodec.CodecException) { lostSync() }
            override fun onOutputFormatChanged(mc: MediaCodec, format: MediaFormat) {}
        }, Handler(ht.looper))
        c.configure(fmt, surface, null, 0)
        c.start()
        codec = c
        needKey = true
        onNeedKeyframe()
    }

    private fun lostSync() {
        needKey = true
        onNeedKeyframe()
    }

    /** [data] = VIDEO payload (flags byte + access unit). Call from one thread. */
    fun feed(data: ByteArray) {
        val c = codec ?: return
        val key = data.isNotEmpty() && (data[0].toInt() and 1) != 0
        if (needKey && !key) return
        val idx = inputs.poll(150, TimeUnit.MILLISECONDS)
        if (idx == null) { lostSync(); return }           // decoder busy: drop and resync
        try {
            val buf = c.getInputBuffer(idx) ?: return
            buf.clear()
            val len = data.size - 1
            if (len > buf.capacity()) { c.queueInputBuffer(idx, 0, 0, 0, 0); lostSync(); return }
            buf.put(data, 1, len)
            c.queueInputBuffer(idx, 0, len, System.nanoTime() / 1000, 0)
            needKey = false
        } catch (_: Exception) {
            lostSync()
        }
    }

    @Synchronized
    private fun releaseCodec() {
        val c = codec ?: return
        codec = null
        try { c.stop() } catch (_: Exception) {}
        try { c.release() } catch (_: Exception) {}
    }

    fun release() {
        releaseCodec()
        ht.quitSafely()
    }
}

/** Opus packets (48 kHz stereo) -> MediaCodec -> AudioTrack, with at most ~200 ms delay. */
class AudioPlayer(private val onError: (String) -> Unit) {
    private val queue = LinkedBlockingQueue<ByteArray>(60)
    @Volatile private var running = true

    init { thread(name = "rd-audio", isDaemon = true) { run() } }

    fun push(packet: ByteArray) {
        if (queue.size > 10) while (queue.size > 2) queue.poll()   // too far behind: catch up
        queue.offer(packet)
    }

    fun stop() {
        running = false
        queue.offer(ByteArray(0))
    }

    private fun opusHead(): ByteArray = ByteBuffer.allocate(19).order(ByteOrder.LITTLE_ENDIAN).apply {
        put("OpusHead".toByteArray()); put(1); put(2)          // version, channels
        putShort(312); putInt(48000); putShort(0); put(0)      // pre-skip, rate, gain, mapping family
    }.array()

    private fun nanos(v: Long) = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN).putLong(v).apply { flip() }

    private fun run() {
        val codec: MediaCodec
        val track: AudioTrack
        try {
            val fmt = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, 48000, 2)
            fmt.setByteBuffer("csd-0", ByteBuffer.wrap(opusHead()))
            fmt.setByteBuffer("csd-1", nanos(312L * 1_000_000_000L / 48000))
            fmt.setByteBuffer("csd-2", nanos(80_000_000L))
            codec = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS)
            codec.configure(fmt, null, null, 0)
            codec.start()
            val min = AudioTrack.getMinBufferSize(48000, AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_16BIT)
            track = AudioTrack.Builder()
                .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
                .setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(48000).setChannelMask(AudioFormat.CHANNEL_OUT_STEREO).build())
                .setBufferSizeInBytes(maxOf(min, 48000 * 4 / 10))
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()
            track.play()
        } catch (e: Exception) {
            onError("Cannot play sound: ${e.message ?: e.javaClass.simpleName}")
            return
        }
        val info = MediaCodec.BufferInfo()
        var pcm = ByteArray(0)
        try {
            while (running) {
                val pkt = queue.take()
                if (!running) break
                if (pkt.isEmpty()) continue
                val i = codec.dequeueInputBuffer(20_000)
                if (i >= 0) {
                    codec.getInputBuffer(i)?.apply { clear(); put(pkt) }
                    codec.queueInputBuffer(i, 0, pkt.size, System.nanoTime() / 1000, 0)
                }
                while (true) {
                    val o = codec.dequeueOutputBuffer(info, 0)
                    if (o < 0) break
                    val buf = codec.getOutputBuffer(o)
                    if (buf != null && info.size > 0) {
                        if (pcm.size < info.size) pcm = ByteArray(info.size)
                        buf.position(info.offset)
                        buf.get(pcm, 0, info.size)
                        track.write(pcm, 0, info.size)
                    }
                    codec.releaseOutputBuffer(o, false)
                }
            }
        } catch (e: Exception) {
            if (running) onError("Sound stopped: ${e.message ?: e.javaClass.simpleName}")
        } finally {
            try { track.stop(); track.release() } catch (_: Exception) {}
            try { codec.stop(); codec.release() } catch (_: Exception) {}
        }
    }
}
