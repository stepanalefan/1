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
        val (q, fps) = Prefs.QUALITY[prefs.quality]
        Session.send(Msg.SET_QUALITY, Json.obj("quality" to q, "fps" to fps))
        thread(name = "rd-recv", isDaemon = true) { receiveLoop() }
        if (prefs.showHelp) remote.postDelayed({ if (!isFinishing) showHelp() }, 600)
    }

    // ------------------------------------------------------------------ UI
    private fun buildUi(): View {
        val root = FrameLayout(this)
        root.setBackgroundColor(Color.BLACK)
        remote = RemoteView(this, this).apply { touchpad = prefs.touchpad }
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

    private fun showMenu(anchor: View) {
        val m = PopupMenu(this, anchor)
        val items = listOf("Fit to screen", "Monitor…", "Quality…", "Send my clipboard",
            "Send a message…", "Ctrl+Alt+Del", if (fullscreen) "Exit fullscreen" else "Fullscreen",
            "Gesture help", "Disconnect")
        items.forEachIndexed { i, s -> m.menu.add(0, i, i, s) }
        m.setOnMenuItemClickListener {
            when (it.itemId) {
                0 -> remote.resetZoom()
                1 -> chooseMonitor()
                2 -> chooseQuality()
                3 -> sendClipboard()
                4 -> sendMessage()
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
                val (q, fps) = Prefs.QUALITY[which]
                Session.send(Msg.SET_QUALITY, Json.obj("quality" to q, "fps" to fps))
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

    private fun sendMessage() {
        val e = EditText(this).apply {
            hint = "Message to the remote user"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE
        }
        AlertDialog.Builder(this).setTitle("Send a message")
            .setView(FrameLayout(this).apply { setPadding(dp(20), dp(8), dp(20), 0); addView(e) })
            .setPositiveButton("Send") { _, _ ->
                val t = e.text.toString()
                if (t.isNotBlank()) Session.send(Msg.CHAT, t.toByteArray(Charsets.UTF_8))
            }
            .setNegativeButton("Cancel", null).show()
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
                        remote.setRemoteSize(o.getInt("w"), o.getInt("h"))
                        runOnUiThread { monitors = list; monitorIndex = o.optInt("monitor") }
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
    override fun mouse(x: Double, y: Double, action: String, button: String?, dx: Int, dy: Int) {
        val p = Json.obj("x" to x, "y" to y, "action" to action, "btn" to button, "dx" to dx, "dy" to dy)
        if (action == "move") Session.sendMove(p, Msg.MOUSE) else Session.send(Msg.MOUSE, p)
    }

    private fun sendKey(name: String, text: String, down: Boolean) =
        Session.send(Msg.KEY, Json.obj("key" to name, "text" to text, "down" to down))

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
