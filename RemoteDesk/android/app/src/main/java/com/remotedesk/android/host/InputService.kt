package com.remotedesk.android.host

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ComponentName
import android.content.Context
import android.graphics.Path
import android.hardware.display.DisplayManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.provider.Settings
import android.util.DisplayMetrics
import android.view.Display
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONObject
import kotlin.math.hypot

/**
 * Performs the remote user's input on this device:
 *  - left button: tap, long press (held > 0.5 s) or swipe/drag along the mouse path
 *  - right button: Back, middle button: Home, wheel: scroll swipe
 *  - keys: typing / Backspace / Enter / Ctrl+C,V,X,A in the focused text field,
 *    Esc = Back, Win = Home
 * Android only allows this through an Accessibility service the user enables.
 */
class InputService : AccessibilityService() {

    companion object {
        @Volatile var instance: InputService? = null
            private set

        fun isEnabled(ctx: Context): Boolean {
            val me = ComponentName(ctx, InputService::class.java).flattenToString()
            val list = Settings.Secure.getString(ctx.contentResolver,
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
            return list.split(':').any { it.equals(me, ignoreCase = true) }
        }
    }

    private val main = Handler(Looper.getMainLooper())
    private var path: Path? = null
    private var downAt = 0L
    private var startX = 0f
    private var startY = 0f
    private var moved = false
    private var ctrl = false

    override fun onServiceConnected() { instance = this }
    override fun onDestroy() { instance = null; super.onDestroy() }
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    private fun screen(): Pair<Int, Int> {
        val m = DisplayMetrics()
        @Suppress("DEPRECATION")
        getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY).getRealMetrics(m)
        return m.widthPixels to m.heightPixels
    }

    // ------------------------------------------------------------- mouse
    fun mouse(data: ByteArray) = main.post {
        val o = try { JSONObject(String(data)) } catch (_: Exception) { return@post }
        val (sw, sh) = screen()
        val x = (o.optDouble("x", 0.5) * sw).toFloat().coerceIn(0f, sw - 1f)
        val y = (o.optDouble("y", 0.5) * sh).toFloat().coerceIn(0f, sh - 1f)
        val btn = if (o.isNull("btn")) "left" else o.optString("btn", "left")
        when (o.optString("action")) {
            "down" -> when (btn) {
                "right" -> performGlobalAction(GLOBAL_ACTION_BACK)
                "middle" -> performGlobalAction(GLOBAL_ACTION_HOME)
                else -> {
                    path = Path().apply { moveTo(x, y) }
                    downAt = SystemClock.uptimeMillis(); startX = x; startY = y; moved = false
                }
            }
            "move" -> path?.let {
                if (hypot(x - startX, y - startY) > 12) moved = true
                if (moved) it.lineTo(x, y)
            }
            "up" -> if (btn == "left") path?.let { p ->
                val held = SystemClock.uptimeMillis() - downAt
                if (moved) gesture(p, held.coerceIn(120, 5_000))
                else gesture(Path().apply { moveTo(startX, startY) }, if (held > 500) held.coerceAtMost(3_000) else 60)
                path = null
            }
            "wheel" -> {
                val dy = o.optInt("dy", 0)
                val dx = o.optInt("dx", 0)
                if (dy == 0 && dx == 0) return@post
                // wheel up (dy > 0) = content moves down = finger swipes down
                val ty = (y + dy * sh * 0.15f).coerceIn(1f, sh - 2f)
                val tx = (x + dx * sw * 0.15f).coerceIn(1f, sw - 2f)
                gesture(Path().apply { moveTo(x, y); lineTo(tx, ty) }, 150)
            }
        }
    }

    private fun gesture(p: Path, durationMs: Long) {
        try {
            dispatchGesture(GestureDescription.Builder()
                .addStroke(GestureDescription.StrokeDescription(p, 0, durationMs.coerceAtLeast(1)))
                .build(), null, null)
        } catch (_: Exception) {
        }
    }

    // ---------------------------------------------------------- keyboard
    fun key(data: ByteArray) = main.post {
        val o = try { JSONObject(String(data)) } catch (_: Exception) { return@post }
        val name = o.optString("key").lowercase()
        val down = o.optBoolean("down")
        if (name == "control") { ctrl = down; return@post }
        if (!down) return@post
        val text = o.optString("text")
        when (name) {
            "escape" -> { performGlobalAction(GLOBAL_ACTION_BACK); return@post }
            "meta" -> { performGlobalAction(GLOBAL_ACTION_HOME); return@post }
            "f1" -> { performGlobalAction(GLOBAL_ACTION_RECENTS); return@post }
            "f2" -> { performGlobalAction(GLOBAL_ACTION_NOTIFICATIONS); return@post }
            "enter" -> { enter(); return@post }
            "backspace" -> { edit { t, s, e -> if (s != e) Triple(t.removeRange(s, e), s, s)
                                   else if (s > 0) Triple(t.removeRange(s - 1, s), s - 1, s - 1) else null }; return@post }
            "delete" -> { edit { t, s, e -> if (s != e) Triple(t.removeRange(s, e), s, s)
                                else if (s < t.length) Triple(t.removeRange(s, s + 1), s, s) else null }; return@post }
            "left" -> { edit { t, s, _ -> Triple(t, maxOf(0, s - 1), maxOf(0, s - 1)) }; return@post }
            "right" -> { edit { t, _, e -> Triple(t, minOf(t.length, e + 1), minOf(t.length, e + 1)) }; return@post }
            "home" -> { edit { t, _, _ -> Triple(t, 0, 0) }; return@post }
            "end" -> { edit { t, _, _ -> Triple(t, t.length, t.length) }; return@post }
            "space" -> { insert(" "); return@post }
            "tab" -> { insert("\t"); return@post }
        }
        if (text.isEmpty()) return@post
        if (ctrl) {
            val node = focused() ?: return@post
            when (text.lowercase()) {
                "c" -> node.performAction(AccessibilityNodeInfo.ACTION_COPY)
                "v" -> node.performAction(AccessibilityNodeInfo.ACTION_PASTE)
                "x" -> node.performAction(AccessibilityNodeInfo.ACTION_CUT)
                "a" -> edit { t, _, _ -> Triple(t, 0, t.length) }
            }
            return@post
        }
        insert(text)
    }

    private fun focused(): AccessibilityNodeInfo? =
        findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.takeIf { it.isEditable }

    private fun insert(s: String) = edit { t, a, b -> Triple(t.substring(0, a) + s + t.substring(b), a + s.length, a + s.length) }

    /** Applies (text, selStart, selEnd) -> (newText, newStart, newEnd) to the focused field. */
    private fun edit(f: (String, Int, Int) -> Triple<String, Int, Int>?) {
        val node = focused() ?: return
        val hint = Build.VERSION.SDK_INT >= 26 && node.isShowingHintText
        val text = if (hint) "" else node.text?.toString() ?: ""
        var s = node.textSelectionStart
        var e = node.textSelectionEnd
        if (s < 0 || e < 0 || s > text.length || e > text.length) { s = text.length; e = text.length }
        if (s > e) { val t = s; s = e; e = t }
        val (nt, ns, ne) = f(text, s, e) ?: return
        if (nt != text) {
            node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply {
                putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, nt)
            })
        }
        node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, Bundle().apply {
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, ns)
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, ne)
        })
    }

    private fun enter() {
        val node = focused() ?: return
        if (Build.VERSION.SDK_INT >= 30 &&
            node.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)) return
        if (node.isMultiLine) insert("\n")
    }
}
