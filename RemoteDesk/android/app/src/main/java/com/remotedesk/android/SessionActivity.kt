package com.remotedesk.android

import android.app.Activity
import android.app.AlertDialog
import android.content.ClipboardManager
import android.content.Context
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.text.InputType
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.view.inputmethod.InputMethodManager
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.TextView
import android.widget.Toast
import android.graphics.Matrix
import android.graphics.SurfaceTexture
import android.view.Surface
import android.view.TextureView
import com.remotedesk.android.protocol.ChatMessage
import com.remotedesk.android.protocol.Json
import com.remotedesk.android.protocol.Msg
import org.json.JSONObject
import kotlin.concurrent.thread

class SessionActivity : Activity(), RemoteView.Input {
    private lateinit var prefs: Prefs
    private lateinit var remote: RemoteView
    private lateinit var toolbar: LinearLayout
    private lateinit var showToolbar: TextView
    private lateinit var keyBar: HorizontalScrollView
    private lateinit var waiting: TextView
    private lateinit var modeButton: TextView
    private var monitors = listOf<String>()
    private var monitorIndex = 0
    private var fullscreen = true
    @Volatile private var ended = false
    private var gotFrame = false
    private lateinit var video: TextureView
    private lateinit var chatPanel: ChatView
    private lateinit var chatButton: TextView
    private lateinit var soundButton: TextView
    private lateinit var roleBadge: TextView
    @Volatile private var decoder: VideoDecoder? = null
    @Volatile private var player: AudioPlayer? = null
    @Volatile private var control = true          // until the desk says otherwise
    @Volatile private var remoteW = 0
    @Volatile private var remoteH = 0
    private var unread = 0
    private var toldNoVideo = false
    private val myName by lazy {
        (try { android.provider.Settings.Global.getString(contentResolver,
            android.provider.Settings.Global.DEVICE_NAME) } catch (_: Exception) { null })
            ?.takeIf { it.isNotBlank() } ?: "${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}"
    }

    /** Sticky modifiers from the key bar: name -> its button. */
    private val sticky = linkedMapOf<String, TextView>()
    private val active = mutableSetOf<String>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ch = Session.channel
        if (ch == null) { finish(); return }
        prefs = Prefs(this)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(buildUi())
        applyFullscreen()
        Session.send(Msg.HELLO, Json.obj("name" to myName))
        sendDisplay()
        setSound(prefs.sound)
        Session.chat.forEach { chatPanel.add(it) }
        thread(name = "rd-recv", isDaemon = true) { receiveLoop() }
        if (prefs.showHelp) remote.postDelayed({ if (!isFinishing) showHelp() }, 600)
    }

    // ------------------------------------------------------------------ UI
    private fun buildUi(): View {
        val root = FrameLayout(this)
        root.setBackgroundColor(Color.BLACK)
        // video mode: hardware-decoded H.264 shown on a TextureView under the input layer
        video = TextureView(this).apply {
            isOpaque = false
            surfaceTextureListener = object : TextureView.SurfaceTextureListener {
                override fun onSurfaceTextureAvailable(st: SurfaceTexture, w: Int, h: Int) {
                    decoder = VideoDecoder(Surface(st)) { Session.send(Msg.KEYFRAME) }
                    if (prefs.videoMode && remoteW > 0) decoder?.configure(remoteW, remoteH)
                }
                override fun onSurfaceTextureSizeChanged(st: SurfaceTexture, w: Int, h: Int) {}
                override fun onSurfaceTextureDestroyed(st: SurfaceTexture): Boolean {
                    decoder?.release(); decoder = null; return true
                }
                override fun onSurfaceTextureUpdated(st: SurfaceTexture) {
                    if (!gotFrame) { gotFrame = true; waiting.visibility = View.GONE }
                }
            }
        }
        root.addView(video, FrameLayout.LayoutParams(MATCH, MATCH))
        remote = RemoteView(this, this).apply {
            touchpad = prefs.touchpad
            videoMode = prefs.videoMode
            onTransform = { ox, oy, sc -> applyVideoTransform(ox, oy, sc) }
        }
        root.addView(remote, FrameLayout.LayoutParams(MATCH, MATCH))

        waiting = text("Waiting for the remote screen…", 15f, 0xFFBBBBBB.toInt())
        root.addView(waiting, FrameLayout.LayoutParams(WRAP, WRAP, Gravity.CENTER))

        // top toolbar
        toolbar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            background = rounded(0xCC202124.toInt(), dp(12).toFloat())
            setPadding(dp(4), dp(2), dp(4), dp(2))
        }
        toolbar.addView(barButton("Keyboard") { toggleKeyboard() })
        toolbar.addView(barButton("Keys") { keyBar.visibility = if (keyBar.visibility == View.VISIBLE) View.GONE else View.VISIBLE })
        modeButton = barButton(modeLabel()) { toggleMode() }
        toolbar.addView(modeButton)
        chatButton = barButton("Chat") { toggleChat() }
        toolbar.addView(chatButton)
        soundButton = barButton("") { setSound(!prefs.sound) }
        toolbar.addView(soundButton)
        toolbar.addView(barButton("More") { v -> showMenu(v) })
        toolbar.addView(barButton("▲") { setToolbarVisible(false) })
        root.addView(toolbar, FrameLayout.LayoutParams(WRAP, WRAP, Gravity.TOP or Gravity.CENTER_HORIZONTAL)
            .apply { topMargin = dp(8) })

        showToolbar = TextView(this).apply {
            text = "▼"
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
            background = rounded(0x99202124.toInt(), dp(12).toFloat())
            setPadding(dp(16), dp(2), dp(16), dp(4))
            visibility = View.GONE
            setOnClickListener { setToolbarVisible(true) }
        }
        root.addView(showToolbar, FrameLayout.LayoutParams(WRAP, WRAP, Gravity.TOP or Gravity.CENTER_HORIZONTAL))

        // special keys bar
        keyBar = HorizontalScrollView(this).apply {
            setBackgroundColor(0xEE202124.toInt())
            isHorizontalScrollBarEnabled = false
            visibility = View.GONE
        }
        val keys = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(dp(4), dp(4), dp(4), dp(4))
        }
        for ((label, name) in listOf("Ctrl" to "control", "Alt" to "alt", "Shift" to "shift", "Win" to "meta")) {
            val b = keyButton(label) { toggleSticky(name) }
            sticky[name] = b
            keys.addView(b)
        }
        val plain = listOf("Esc" to "escape", "Tab" to "tab", "←" to "left", "↑" to "up", "↓" to "down",
            "→" to "right", "Home" to "home", "End" to "end", "PgUp" to "pageup", "PgDn" to "pagedown",
            "Del" to "delete", "Ins" to "insert", "PrtSc" to "printscreen") +
            (1..12).map { "F$it" to "f$it" }
        for ((label, name) in plain) keys.addView(keyButton(label) { tapKey(name) })
        keys.addView(keyButton("Ctrl+Alt+Del") { ctrlAltDel() })
        keyBar.addView(keys)
        root.addView(keyBar, FrameLayout.LayoutParams(MATCH, WRAP, Gravity.BOTTOM))

        roleBadge = text("", 13f, Color.WHITE).apply {
            background = rounded(0xCCB06A00.toInt(), dp(10).toFloat())
            setPadding(dp(12), dp(6), dp(12), dp(6))
            visibility = View.GONE
        }
        root.addView(roleBadge, FrameLayout.LayoutParams(WRAP, WRAP, Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL)
            .apply { bottomMargin = dp(64) })

        chatPanel = ChatView(this) { sendChat(it) }.apply { visibility = View.GONE; elevation = dp(8).toFloat() }
        root.addView(chatPanel, FrameLayout.LayoutParams(MATCH, MATCH).apply { topMargin = dp(56) })
        // stay clear of the status / nav bars, notch and keyboard (zero while fullscreen)
        root.onSystemInsets { l, t, r, b -> root.setPadding(l, t, r, b) }
        return root
    }

    private fun barButton(label: String, onClick: (View) -> Unit) = TextView(this).apply {
        text = label
        setTextColor(Color.WHITE)
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
        setPadding(dp(12), dp(10), dp(12), dp(10))
        setOnClickListener { onClick(it) }
    }

    private fun keyButton(label: String, onClick: () -> Unit) = TextView(this).apply {
        text = label
        setTextColor(Color.WHITE)
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
        typeface = Typeface.DEFAULT_BOLD
        gravity = Gravity.CENTER
        minWidth = dp(48)
        background = rounded(0xFF3C4043.toInt(), dp(6).toFloat())
        setPadding(dp(10), dp(10), dp(10), dp(10))
        layoutParams = LinearLayout.LayoutParams(WRAP, WRAP).apply { setMargins(dp(3), 0, dp(3), 0) }
        setOnClickListener { onClick() }
    }

    private fun setToolbarVisible(v: Boolean) {
        toolbar.visibility = if (v) View.VISIBLE else View.GONE
        showToolbar.visibility = if (v) View.GONE else View.VISIBLE
    }

    private fun modeLabel() = if (prefs.touchpad) "Mouse: Touchpad" else "Mouse: Direct"

    private fun toggleMode() {
        prefs.touchpad = !prefs.touchpad
        remote.touchpad = prefs.touchpad
        modeButton.text = modeLabel()
        Toast.makeText(this, if (prefs.touchpad) "Touchpad: drag to move the pointer, tap to click"
            else "Direct touch: tap where you want to click", Toast.LENGTH_SHORT).show()
    }

    private var keyboardShown = false

    private fun toggleKeyboard() {
        val imm = getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
        val visible = if (android.os.Build.VERSION.SDK_INT >= 30)
            remote.rootWindowInsets?.isVisible(android.view.WindowInsets.Type.ime()) ?: keyboardShown
        else keyboardShown
        if (visible) {
            imm.hideSoftInputFromWindow(remote.windowToken, 0)
            keyboardShown = false
        } else {
            remote.requestFocus()
            imm.showSoftInput(remote, 0)
            keyboardShown = true
        }
    }

    @Suppress("DEPRECATION")
    private fun applyFullscreen() {
        window.decorView.systemUiVisibility = if (fullscreen)
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or
                    View.SYSTEM_UI_FLAG_FULLSCREEN
        else 0
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) applyFullscreen()
    }

    // ---------------------------------------------------------- media
    private fun applyVideoTransform(ox: Float, oy: Float, sc: Float) {
        if (remoteW == 0 || video.width == 0) return
        val m = Matrix()
        // the TextureView stretches the video to its own size; map it onto the zoomed remote screen
        m.setScale(remoteW * sc / video.width, remoteH * sc / video.height)
        m.postTranslate(ox, oy)
        video.setTransform(m)
    }

    private fun sendDisplay() {
        val (q, fps) = Prefs.QUALITY[prefs.quality]
        Session.send(Msg.SET_QUALITY, Json.obj("quality" to q, "fps" to fps + (if (prefs.videoMode) 10 else 0),
            "mode" to if (prefs.videoMode) "video" else "jpeg"))
    }

    private fun setSound(on: Boolean) {
        prefs.sound = on
        soundButton.text = if (on) "Sound: on" else "Sound: off"
        Session.send(Msg.AUDIO_CTRL, Json.obj("enabled" to on))
        if (on && player == null) {
            player = AudioPlayer { msg -> runOnUiThread { Toast.makeText(this, msg, Toast.LENGTH_LONG).show() } }
        } else if (!on) {
            player?.stop(); player = null
        }
    }

    private fun choosePicture() {
        AlertDialog.Builder(this).setTitle("Picture")
            .setSingleChoiceItems(arrayOf("JPEG tiles — sharp text, low traffic when idle",
                "Video stream (H.264) — smooth motion and video"), if (prefs.videoMode) 1 else 0) { d, which ->
                prefs.videoMode = which == 1
                remote.videoMode = prefs.videoMode
                if (prefs.videoMode && remoteW > 0) decoder?.configure(remoteW, remoteH)
                sendDisplay()
                d.dismiss()
            }.show()
    }

    // ----------------------------------------------------------- chat
    private fun toggleChat() {
        val show = chatPanel.visibility != View.VISIBLE
        chatPanel.visibility = if (show) View.VISIBLE else View.GONE
        if (show) {
            unread = 0
            chatButton.text = "Chat"
            chatPanel.focusInput()
        } else {
            (getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager)
                .hideSoftInputFromWindow(chatPanel.windowToken, 0)
        }
    }

    private fun sendChat(text: String) {
        val m = ChatMessage(myName, text, System.currentTimeMillis(), mine = true)
        Session.send(Msg.CHAT, m.toJson())
        Session.chat += m
        chatPanel.add(m)
    }

    private fun onChat(m: ChatMessage) {
        Session.chat += m
        chatPanel.add(m)
        if (chatPanel.visibility != View.VISIBLE) {
            unread++
            chatButton.text = "Chat ($unread)"
            Toast.makeText(this, "${m.from}: ${m.text}", Toast.LENGTH_LONG).show()
        }
    }

    private fun onRole(o: JSONObject) {
        control = o.optBoolean("control", true)
        val n = o.optJSONArray("viewers")?.length() ?: 1
        val who = o.optString("controller").takeIf { it.isNotEmpty() && it != "null" }
        roleBadge.visibility = if (control) View.GONE else View.VISIBLE
        roleBadge.text = "View only" + (who?.let { " — $it has control" } ?: "") +
                (if (n > 1) " · $n connected" else "")
    }

    private fun showMenu(anchor: View) {
        val m = PopupMenu(this, anchor)
        val items = listOf("Fit to screen", "Monitor…", "Quality…", "Send my clipboard",
            "Picture: JPEG / video…", "Ctrl+Alt+Del", if (fullscreen) "Exit fullscreen" else "Fullscreen",
            "Gesture help", "Disconnect")
        items.forEachIndexed { i, s -> m.menu.add(0, i, i, s) }
        m.setOnMenuItemClickListener {
            when (it.itemId) {
                0 -> remote.resetZoom()
                1 -> chooseMonitor()
                2 -> chooseQuality()
                3 -> sendClipboard()
                4 -> choosePicture()
                5 -> ctrlAltDel()
                6 -> { fullscreen = !fullscreen; applyFullscreen() }
                7 -> showHelp()
                8 -> confirmDisconnect()
            }
            true
        }
        m.show()
    }

    private fun chooseMonitor() {
        if (monitors.size < 2) {
            Toast.makeText(this, "The remote computer has one monitor", Toast.LENGTH_SHORT).show()
            return
        }
        AlertDialog.Builder(this).setTitle("Monitor")
            .setSingleChoiceItems(monitors.toTypedArray(), monitorIndex) { d, which ->
                Session.send(Msg.SET_MONITOR, Json.obj("index" to which))
                d.dismiss()
            }.show()
    }

    private fun chooseQuality() {
        AlertDialog.Builder(this).setTitle("Quality")
            .setSingleChoiceItems(Prefs.QUALITY_NAMES, prefs.quality) { d, which ->
                prefs.quality = which
                sendDisplay()
                d.dismiss()
            }.show()
    }

    private fun sendClipboard() {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        val text = cm.primaryClip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(this)?.toString()
        if (text.isNullOrEmpty()) {
            Toast.makeText(this, "Your clipboard is empty", Toast.LENGTH_SHORT).show()
            return
        }
        Session.send(Msg.CLIPBOARD, text.toByteArray(Charsets.UTF_8))
        Toast.makeText(this, "Clipboard sent — paste it on the remote computer", Toast.LENGTH_SHORT).show()
    }

    private fun showHelp() {
        val touch = """
            Touchpad mode
              • Drag one finger — move the pointer
              • Tap — left click, double tap — double click
              • Double tap and drag — drag / select
              • Long press or two-finger tap — right click

            Direct touch mode
              • Tap — click at that spot
              • Drag — drag / select
              • Long press — right click

            Both modes
              • Two-finger drag — scroll
              • Pinch — zoom in / out (move while pinching to pan)
              • “Keys” — Ctrl, Alt, Win, Esc, arrows, F-keys
              • Ctrl / Alt / Shift / Win stay pressed until the next key
        """.trimIndent()
        AlertDialog.Builder(this).setTitle("Gestures").setMessage(touch)
            .setPositiveButton("OK", null)
            .setNeutralButton("Don't show again") { _, _ -> prefs.showHelp = false }
            .show()
    }

    private fun confirmDisconnect() {
        AlertDialog.Builder(this).setTitle("Disconnect from ${Session.title}?")
            .setPositiveButton("Disconnect") { _, _ -> finish() }
            .setNegativeButton("Cancel", null).show()
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() = confirmDisconnect()

    override fun onDestroy() {
        super.onDestroy()
        ended = true
        player?.stop(); player = null
        decoder?.release(); decoder = null
        if (isFinishing) Session.close()
    }

    // ------------------------------------------------------------ network
    private fun receiveLoop() {
        val ch = Session.channel ?: return
        val opts = BitmapFactory.Options()
        try {
            while (true) {
                val (type, data) = ch.recv()
                when (type) {
                    Msg.FRAME_INFO -> {
                        val o = JSONObject(String(data, Charsets.UTF_8))
                        val arr = o.optJSONArray("monitors")
                        val list = (0 until (arr?.length() ?: 0)).map { i ->
                            val m = arr!!.getJSONObject(i)
                            "Monitor ${i + 1}  (${m.optInt("w")}×${m.optInt("h")})"
                        }
                        remoteW = o.getInt("w"); remoteH = o.getInt("h")
                        remote.setRemoteSize(remoteW, remoteH)
                        val isVideo = o.optString("mode") == "video"
                        if (isVideo) decoder?.configure(remoteW, remoteH)
                        runOnUiThread {
                            monitors = list; monitorIndex = o.optInt("monitor")
                            if (!isVideo && prefs.videoMode && !o.has("mode") && !toldNoVideo) {
                                toldNoVideo = true      // e.g. a phone desk: JPEG tiles only
                                Toast.makeText(this, "This desk sends JPEG tiles only", Toast.LENGTH_SHORT).show()
                            }
                            remote.videoMode = isVideo
                            video.visibility = if (isVideo) View.VISIBLE else View.INVISIBLE
                            remote.invalidate()
                        }
                    }
                    Msg.VIDEO -> decoder?.feed(data)
                    Msg.AUDIO -> player?.push(data)
                    Msg.AUDIO_CTRL -> {
                        val o = JSONObject(String(data, Charsets.UTF_8))
                        if (!o.optBoolean("available", true)) runOnUiThread {
                            Toast.makeText(this, o.optString("error", "No sound from the remote computer"),
                                Toast.LENGTH_LONG).show()
                        }
                    }
                    Msg.ROLE -> {
                        val o = JSONObject(String(data, Charsets.UTF_8))
                        runOnUiThread { onRole(o) }
                    }
                    Msg.CHAT -> {
                        val m = ChatMessage.parse(data, Session.title)
                        runOnUiThread { onChat(m) }
                    }
                    Msg.TILE -> {
                        val x = ((data[0].toInt() and 0xff) shl 8) or (data[1].toInt() and 0xff)
                        val y = ((data[2].toInt() and 0xff) shl 8) or (data[3].toInt() and 0xff)
                        val bmp = BitmapFactory.decodeByteArray(data, 4, data.size - 4, opts)
                        if (bmp != null) {
                            remote.drawTile(x, y, bmp)
                            bmp.recycle()
                        }
                    }
                    Msg.FRAME_END -> {
                        remote.frameDone()
                        if (!gotFrame) { gotFrame = true; runOnUiThread { waiting.visibility = View.GONE } }
                    }
                    Msg.BYE -> break
                }
            }
        } catch (_: Exception) {
        }
        runOnUiThread {
            if (ended || isFinishing) return@runOnUiThread
            AlertDialog.Builder(this).setTitle("Session ended")
                .setMessage("The connection to ${Session.title} was closed.")
                .setCancelable(false)
                .setPositiveButton("OK") { _, _ -> finish() }
                .show()
        }
    }

    // ------------------------------------------------------------ input
    private var warnedViewOnly = 0L

    private fun viewOnly(): Boolean {
        if (control) return false
        val now = System.currentTimeMillis()
        if (now - warnedViewOnly > 4000) {
            warnedViewOnly = now
            runOnUiThread { Toast.makeText(this, "View only — another user has control", Toast.LENGTH_SHORT).show() }
        }
        return true
    }

    override fun mouse(x: Double, y: Double, action: String, button: String?, dx: Int, dy: Int) {
        if (action != "move" && viewOnly()) return
        if (!control) return
        val p = Json.obj("x" to x, "y" to y, "action" to action, "btn" to button, "dx" to dx, "dy" to dy)
        if (action == "move") Session.sendMove(p, Msg.MOUSE) else Session.send(Msg.MOUSE, p)
    }

    private fun sendKey(name: String, text: String, down: Boolean) {
        if (down && viewOnly()) return
        if (!control) return
        Session.send(Msg.KEY, Json.obj("key" to name, "text" to text, "down" to down))
    }

    override fun typeText(text: String) {
        sendKey("", text, true)
        sendKey("", text, false)
        releaseSticky()
    }

    override fun key(name: String, down: Boolean) {
        sendKey(name, "", down)
        if (!down && name !in MODIFIERS) releaseSticky()
    }

    override fun hardwareChar(ch: Char, down: Boolean) {
        sendKey("", ch.toString(), down)
        if (!down) releaseSticky()
    }

    private fun tapKey(name: String) {
        key(name, true)
        key(name, false)
    }

    private fun toggleSticky(name: String) {
        if (name in active) {
            active.remove(name)
            sendKey(name, "", false)
        } else {
            active.add(name)
            sendKey(name, "", true)
        }
        updateSticky()
    }

    private fun releaseSticky() {
        if (active.isEmpty()) return
        active.toList().reversed().forEach { sendKey(it, "", false) }
        active.clear()
        runOnUiThread { updateSticky() }
    }

    private fun updateSticky() {
        for ((name, b) in sticky)
            b.background = rounded(if (name in active) ACCENT else 0xFF3C4043.toInt(), dp(6).toFloat())
    }

    private fun ctrlAltDel() {
        listOf("control", "alt", "delete").forEach { sendKey(it, "", true) }
        listOf("delete", "alt", "control").forEach { sendKey(it, "", false) }
        Toast.makeText(this, "Ctrl+Alt+Del sent (Windows may block it unless RemoteDesk runs as a service)",
            Toast.LENGTH_LONG).show()
    }

    companion object {
        private val MODIFIERS = setOf("control", "alt", "shift", "meta")
    }
}
