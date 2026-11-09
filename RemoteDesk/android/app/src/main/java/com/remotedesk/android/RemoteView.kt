package com.remotedesk.android

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.os.SystemClock
import android.text.InputType
import android.view.HapticFeedbackConstants
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.inputmethod.BaseInputConnection
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputConnection
import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min

/**
 * Shows the remote screen and turns touches / keys into remote input.
 *
 * Touchpad mode: drag = move pointer, tap = click, double-tap-and-drag = drag,
 * long press or two-finger tap = right click.
 * Direct mode: tap = click there, drag = drag, long press = right click.
 * Both: two-finger drag = scroll wheel, pinch = zoom (+ pan while pinching).
 */
class RemoteView(context: Context, private val input: Input) : View(context) {

    interface Input {
        /** x, y are 0..1 of the remote screen; action = move|down|up|wheel. */
        fun mouse(x: Double, y: Double, action: String, button: String? = null, dx: Int = 0, dy: Int = 0)
        fun typeText(text: String)
        fun key(name: String, down: Boolean)
        fun hardwareChar(ch: Char, down: Boolean)
    }

    // ------------------------------------------------------------ frame
    private val lock = Any()
    private var frame: Bitmap? = null
    private var frameCanvas: Canvas? = null
    private val bitmapPaint = Paint(Paint.FILTER_BITMAP_FLAG)

    val remoteWidth get() = frame?.width ?: 0
    val remoteHeight get() = frame?.height ?: 0

    /** Called from the network thread. */
    fun setRemoteSize(w: Int, h: Int) {
        synchronized(lock) {
            val keep = frame?.takeIf { it.width == w && it.height == h }
            if (keep == null) {
                frame?.recycle()
                frame = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888).also {
                    it.eraseColor(Color.BLACK)
                    frameCanvas = Canvas(it)
                }
            }
        }
        post {
            curX = w / 2f; curY = h / 2f
            resetZoom()
        }
    }

    /** Called from the network thread. */
    fun drawTile(x: Int, y: Int, tile: Bitmap) {
        synchronized(lock) { frameCanvas?.drawBitmap(tile, x.toFloat(), y.toFloat(), null) }
    }

    fun frameDone() = postInvalidateOnAnimation()

    // ------------------------------------------------------------ view transform
    private var zoom = 1f                 // 1 = fit to view
    private var offX = 0f
    private var offY = 0f
    private val fitScale: Float
        get() {
            val w = remoteWidth; val h = remoteHeight
            if (w == 0 || h == 0 || width == 0 || height == 0) return 1f
            return min(width.toFloat() / w, height.toFloat() / h)
        }
    private val scale get() = fitScale * zoom

    fun resetZoom() {
        zoom = 1f
        clampOffsets(center = true)
        invalidate()
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        clampOffsets(center = zoom <= 1f)
        ensureCursorVisible()
    }

    private fun clampOffsets(center: Boolean = false) {
        val iw = remoteWidth * scale
        val ih = remoteHeight * scale
        offX = if (iw <= width || center) (width - iw) / 2 else offX.coerceIn(width - iw, 0f)
        offY = if (ih <= height || center) (height - ih) / 2 else offY.coerceIn(height - ih, 0f)
    }

    private fun toRemoteX(sx: Float) = (sx - offX) / scale
    private fun toRemoteY(sy: Float) = (sy - offY) / scale

    // ------------------------------------------------------------ pointer
    var touchpad = true
        set(v) { field = v; invalidate() }
    private var curX = 0f                 // remote pixel coordinates
    private var curY = 0f
    private val cursorPath = Path()
    private val cursorFill = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.BLACK }
    private val cursorStroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE; style = Paint.Style.STROKE; strokeWidth = dp(2).toFloat()
        strokeJoin = Paint.Join.ROUND
    }

    private fun sendPointer(action: String, button: String? = null) {
        val w = remoteWidth; val h = remoteHeight
        if (w == 0 || h == 0) return
        input.mouse((curX / w).toDouble().coerceIn(0.0, 1.0), (curY / h).toDouble().coerceIn(0.0, 1.0),
            action, button)
    }

    private fun moveCursorTo(rx: Float, ry: Float) {
        curX = rx.coerceIn(0f, max(0, remoteWidth - 1).toFloat())
        curY = ry.coerceIn(0f, max(0, remoteHeight - 1).toFloat())
        sendPointer("move")
    }

    private fun click(button: String) {
        sendPointer("down", button)
        sendPointer("up", button)
    }

    private fun ensureCursorVisible() {
        if (!touchpad || zoom <= 1f) return
        val margin = dp(48).toFloat()
        val sx = curX * scale + offX
        val sy = curY * scale + offY
        if (sx < margin) offX += margin - sx
        if (sx > width - margin) offX -= sx - (width - margin)
        if (sy < margin) offY += margin - sy
        if (sy > height - margin) offY -= sy - (height - margin)
        clampOffsets()
    }

    /** Video mode: the picture comes from a TextureView underneath; this view only
     *  handles input and draws the pointer, and reports zoom/pan through [onTransform]. */
    var videoMode = false
        set(v) { field = v; invalidate() }
    var onTransform: ((offX: Float, offY: Float, scale: Float) -> Unit)? = null
    private var lastTransform = Triple(Float.NaN, 0f, 0f)

    override fun onDraw(canvas: Canvas) {
        val t = Triple(offX, offY, scale)
        if (t != lastTransform) { lastTransform = t; onTransform?.invoke(offX, offY, scale) }
        if (!videoMode) {
            canvas.drawColor(0xFF1E1E1E.toInt())
            synchronized(lock) {
                val f = frame ?: return
                canvas.save()
                canvas.translate(offX, offY)
                canvas.scale(scale, scale)
                canvas.drawBitmap(f, 0f, 0f, bitmapPaint)
                canvas.restore()
            }
        }
        if (touchpad) {
            val x = curX * scale + offX
            val y = curY * scale + offY
            val u = dp(1).toFloat()
            cursorPath.reset()
            cursorPath.moveTo(x, y)
            cursorPath.lineTo(x, y + 17 * u)
            cursorPath.lineTo(x + 4.5f * u, y + 13 * u)
            cursorPath.lineTo(x + 8 * u, y + 20 * u)
            cursorPath.lineTo(x + 10.5f * u, y + 19 * u)
            cursorPath.lineTo(x + 7.5f * u, y + 12 * u)
            cursorPath.lineTo(x + 13 * u, y + 12 * u)
            cursorPath.close()
            canvas.drawPath(cursorPath, cursorStroke)
            canvas.drawPath(cursorPath, cursorFill)
        }
    }

    // ------------------------------------------------------------ gestures
    private val slop = ViewConfiguration.get(context).scaledTouchSlop.toFloat()
    private val longPressMs = ViewConfiguration.getLongPressTimeout().toLong()
    private val doubleTapMs = ViewConfiguration.getDoubleTapTimeout().toLong()
    private val pointerSpeed = 1.6f

    private enum class Two { UNDECIDED, ZOOM, SCROLL }

    private var downX = 0f
    private var downY = 0f
    private var lastX = 0f
    private var lastY = 0f
    private var downTime = 0L
    private var moved = false
    private var maxPointers = 0
    private var multiDone = false          // a 2-finger gesture happened; ignore the rest
    private var dragging = false
    private var longFired = false
    private var lastTapUp = 0L
    private var lastTapX = 0f
    private var lastTapY = 0f
    private var tapDrag = false            // touchpad: double-tap-and-drag
    private var two = Two.UNDECIDED
    private var span0 = 0f
    private var lastSpan = 0f
    private var focusX0 = 0f
    private var focusY0 = 0f
    private var lastFocusX = 0f
    private var lastFocusY = 0f
    private var scrollAcc = 0f

    private val longPress = Runnable {
        if (!moved && maxPointers == 1 && !dragging) {
            longFired = true
            performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
            if (!touchpad) moveCursorTo(toRemoteX(downX), toRemoteY(downY))
            click("right")
        }
    }

    private fun span(e: MotionEvent): Float =
        hypot(e.getX(0) - e.getX(1), e.getY(0) - e.getY(1))

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(e: MotionEvent): Boolean {
        if (remoteWidth == 0) return true
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                downX = e.x; downY = e.y; lastX = e.x; lastY = e.y
                downTime = SystemClock.uptimeMillis()
                moved = false; maxPointers = 1; multiDone = false; dragging = false
                longFired = false; two = Two.UNDECIDED; scrollAcc = 0f
                tapDrag = touchpad && downTime - lastTapUp < doubleTapMs &&
                        hypot(e.x - lastTapX, e.y - lastTapY) < slop * 4
                if (!tapDrag) postDelayed(longPress, longPressMs)
            }
            MotionEvent.ACTION_POINTER_DOWN -> {
                maxPointers = max(maxPointers, e.pointerCount)
                removeCallbacks(longPress)
                if (dragging) { sendPointer("up", "left"); dragging = false }
                if (e.pointerCount == 2) {
                    two = Two.UNDECIDED
                    span0 = span(e); lastSpan = span0
                    focusX0 = (e.getX(0) + e.getX(1)) / 2; focusY0 = (e.getY(0) + e.getY(1)) / 2
                    lastFocusX = focusX0; lastFocusY = focusY0
                }
            }
            MotionEvent.ACTION_MOVE -> {
                if (e.pointerCount >= 2) twoFingerMove(e)
                else if (!multiDone) oneFingerMove(e)
            }
            MotionEvent.ACTION_POINTER_UP -> {
                if (e.pointerCount - 1 < 2) multiDone = true
            }
            MotionEvent.ACTION_UP -> {
                removeCallbacks(longPress)
                val now = SystemClock.uptimeMillis()
                when {
                    dragging -> { sendPointer("up", "left"); dragging = false }
                    maxPointers == 2 && two == Two.UNDECIDED && now - downTime < 400 -> click("right")
                    maxPointers == 1 && !moved && !longFired -> {
                        if (!touchpad) moveCursorTo(toRemoteX(e.x), toRemoteY(e.y))
                        click("left")
                        lastTapUp = now; lastTapX = e.x; lastTapY = e.y
                    }
                }
                tapDrag = false
            }
            MotionEvent.ACTION_CANCEL -> {
                removeCallbacks(longPress)
                if (dragging) { sendPointer("up", "left"); dragging = false }
            }
        }
        return true
    }

    private fun oneFingerMove(e: MotionEvent) {
        if (!moved && hypot(e.x - downX, e.y - downY) > slop) {
            moved = true
            removeCallbacks(longPress)
            if (touchpad && tapDrag) { dragging = true; sendPointer("down", "left") }
            if (!touchpad) {
                moveCursorTo(toRemoteX(downX), toRemoteY(downY))
                sendPointer("down", "left")
                dragging = true
            }
        }
        if (!moved) return
        if (touchpad) {
            val k = pointerSpeed / scale
            moveCursorTo(curX + (e.x - lastX) * k, curY + (e.y - lastY) * k)
            ensureCursorVisible()
        } else {
            moveCursorTo(toRemoteX(e.x), toRemoteY(e.y))
        }
        lastX = e.x; lastY = e.y
        invalidate()
    }

    private fun twoFingerMove(e: MotionEvent) {
        val sp = span(e)
        val fx = (e.getX(0) + e.getX(1)) / 2
        val fy = (e.getY(0) + e.getY(1)) / 2
        if (two == Two.UNDECIDED) {
            if (abs(sp - span0) > slop * 2) two = Two.ZOOM
            else if (hypot(fx - focusX0, fy - focusY0) > slop * 1.5f) two = Two.SCROLL
            if (two != Two.UNDECIDED) moved = true
        }
        when (two) {
            Two.ZOOM -> {
                val old = scale
                zoom = (zoom * (sp / max(1f, lastSpan))).coerceIn(1f, 8f)
                val ns = scale
                // keep the content under the fingers in place, and pan with them
                offX = fx - (lastFocusX - offX) * (ns / old)
                offY = fy - (lastFocusY - offY) * (ns / old)
                clampOffsets()
                invalidate()
            }
            Two.SCROLL -> {
                scrollAcc += fy - lastFocusY
                val step = dp(28).toFloat()
                while (abs(scrollAcc) >= step) {
                    val dir = if (scrollAcc > 0) 1 else -1     // fingers down = scroll up
                    val w = remoteWidth; val h = remoteHeight
                    val px = if (touchpad) curX else toRemoteX(fx)
                    val py = if (touchpad) curY else toRemoteY(fy)
                    input.mouse((px / w).toDouble().coerceIn(0.0, 1.0), (py / h).toDouble().coerceIn(0.0, 1.0),
                        "wheel", null, 0, dir)
                    scrollAcc -= dir * step
                }
            }
            Two.UNDECIDED -> {}
        }
        lastSpan = sp; lastFocusX = fx; lastFocusY = fy
    }

    // ------------------------------------------------------------ keyboard
    init {
        isFocusable = true
        isFocusableInTouchMode = true
    }

    override fun onCheckIsTextEditor() = true

    override fun onCreateInputConnection(outAttrs: EditorInfo): InputConnection {
        outAttrs.inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD or
                InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        outAttrs.imeOptions = EditorInfo.IME_FLAG_NO_EXTRACT_UI or EditorInfo.IME_FLAG_NO_FULLSCREEN or
                EditorInfo.IME_ACTION_NONE
        return object : BaseInputConnection(this, false) {
            private var composing = ""

            private fun type(s: String) {
                for (ch in s) {
                    if (ch == '\n') { tap("enter") } else input.typeText(ch.toString())
                }
            }

            private fun tap(name: String) { input.key(name, true); input.key(name, false) }

            override fun commitText(text: CharSequence, newCursorPosition: Int): Boolean {
                replaceComposing(text.toString())
                composing = ""
                return true
            }

            override fun setComposingText(text: CharSequence, newCursorPosition: Int): Boolean {
                replaceComposing(text.toString())
                composing = text.toString()
                return true
            }

            /** Sends only the difference between the old and new composing text. */
            private fun replaceComposing(new: String) {
                val common = composing.commonPrefixWith(new).length
                repeat(composing.length - common) { tap("backspace") }
                type(new.substring(common))
            }

            override fun finishComposingText(): Boolean { composing = ""; return true }

            override fun deleteSurroundingText(beforeLength: Int, afterLength: Int): Boolean {
                repeat(beforeLength) { tap("backspace") }
                repeat(afterLength) { tap("delete") }
                return true
            }

            override fun sendKeyEvent(event: KeyEvent): Boolean {
                if (!handleKey(event)) return super.sendKeyEvent(event)
                return true
            }

            override fun performEditorAction(actionCode: Int): Boolean { tap("enter"); return true }
        }
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent) =
        if (keyCode == KeyEvent.KEYCODE_BACK) super.onKeyDown(keyCode, event) else handleKey(event) || super.onKeyDown(keyCode, event)

    override fun onKeyUp(keyCode: Int, event: KeyEvent) =
        if (keyCode == KeyEvent.KEYCODE_BACK) super.onKeyUp(keyCode, event) else handleKey(event) || super.onKeyUp(keyCode, event)

    /** Hardware keyboards and IME key events. */
    private fun handleKey(event: KeyEvent): Boolean {
        val down = event.action == KeyEvent.ACTION_DOWN
        if (event.action == KeyEvent.ACTION_MULTIPLE) {
            event.characters?.let { s -> s.forEach { input.typeText(it.toString()) } }
            return true
        }
        val name = KEYS[event.keyCode]
        if (name != null) { input.key(name, down); return true }
        // Shift / Caps Lock shape the character; Ctrl / Alt are sent as separate keys
        val ch = event.getUnicodeChar(event.metaState and
                (KeyEvent.META_SHIFT_MASK or KeyEvent.META_CAPS_LOCK_ON))
        if (ch != 0 && (ch and KeyCharacterMapCombining) == 0) {
            input.hardwareChar(ch.toChar(), down)
            return true
        }
        return false
    }

    companion object {
        private const val KeyCharacterMapCombining = 0x80000000.toInt()
        val KEYS = mapOf(
            KeyEvent.KEYCODE_ENTER to "enter", KeyEvent.KEYCODE_NUMPAD_ENTER to "enter",
            KeyEvent.KEYCODE_DEL to "backspace", KeyEvent.KEYCODE_FORWARD_DEL to "delete",
            KeyEvent.KEYCODE_TAB to "tab", KeyEvent.KEYCODE_ESCAPE to "escape",
            KeyEvent.KEYCODE_DPAD_LEFT to "left", KeyEvent.KEYCODE_DPAD_RIGHT to "right",
            KeyEvent.KEYCODE_DPAD_UP to "up", KeyEvent.KEYCODE_DPAD_DOWN to "down",
            KeyEvent.KEYCODE_MOVE_HOME to "home", KeyEvent.KEYCODE_MOVE_END to "end",
            KeyEvent.KEYCODE_PAGE_UP to "pageup", KeyEvent.KEYCODE_PAGE_DOWN to "pagedown",
            KeyEvent.KEYCODE_INSERT to "insert", KeyEvent.KEYCODE_SPACE to "space",
            KeyEvent.KEYCODE_SHIFT_LEFT to "shift", KeyEvent.KEYCODE_SHIFT_RIGHT to "shift",
            KeyEvent.KEYCODE_CTRL_LEFT to "control", KeyEvent.KEYCODE_CTRL_RIGHT to "control",
            KeyEvent.KEYCODE_ALT_LEFT to "alt", KeyEvent.KEYCODE_ALT_RIGHT to "alt",
            KeyEvent.KEYCODE_META_LEFT to "meta", KeyEvent.KEYCODE_META_RIGHT to "meta",
            KeyEvent.KEYCODE_CAPS_LOCK to "capslock", KeyEvent.KEYCODE_SYSRQ to "printscreen",
            KeyEvent.KEYCODE_MENU to "menu",
            KeyEvent.KEYCODE_F1 to "f1", KeyEvent.KEYCODE_F2 to "f2", KeyEvent.KEYCODE_F3 to "f3",
            KeyEvent.KEYCODE_F4 to "f4", KeyEvent.KEYCODE_F5 to "f5", KeyEvent.KEYCODE_F6 to "f6",
            KeyEvent.KEYCODE_F7 to "f7", KeyEvent.KEYCODE_F8 to "f8", KeyEvent.KEYCODE_F9 to "f9",
            KeyEvent.KEYCODE_F10 to "f10", KeyEvent.KEYCODE_F11 to "f11", KeyEvent.KEYCODE_F12 to "f12",
        )
    }
}
