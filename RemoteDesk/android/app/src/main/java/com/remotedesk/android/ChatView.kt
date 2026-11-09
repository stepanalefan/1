package com.remotedesk.android

import android.content.Context
import android.graphics.Color
import android.text.InputType
import android.view.Gravity
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.remotedesk.android.protocol.ChatMessage
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Chat panel: message bubbles (sender + time) and an input line. */
class ChatView(context: Context, private val onSend: (String) -> Unit) : LinearLayout(context) {
    private val list = LinearLayout(context).apply { orientation = VERTICAL; setPadding(dp(8), dp(8), dp(8), dp(8)) }
    private val scroll = ScrollView(context).apply { addView(list); isFillViewport = true }
    private val input = EditText(context)
    private val time = SimpleDateFormat("HH:mm", Locale.getDefault())

    init {
        orientation = VERTICAL
        setBackgroundColor(0xFFF3F4F6.toInt())
        addView(scroll, LayoutParams(MATCH, 0, 1f))
        val row = LinearLayout(context).apply {
            orientation = HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(Color.WHITE)
            setPadding(dp(8), dp(6), dp(8), dp(6))
        }
        input.apply {
            hint = "Type a message…"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
            imeOptions = EditorInfo.IME_ACTION_SEND
            setOnEditorActionListener { _, _, _ -> send(); true }
        }
        row.addView(input, LayoutParams(0, WRAP, 1f))
        row.addView(context.button("Send", primary = true) { send() }, LayoutParams(WRAP, WRAP))
        addView(row, LayoutParams(MATCH, WRAP))
        info("Messages are end-to-end encrypted.")
    }

    private fun send() {
        val t = input.text.toString().trim()
        if (t.isEmpty()) return
        input.setText("")
        onSend(t)
    }

    fun add(m: ChatMessage) {
        val box = LinearLayout(context).apply {
            orientation = VERTICAL
            gravity = if (m.mine) Gravity.END else Gravity.START
        }
        box.addView(context.text("${if (m.mine) "You" else m.from} · ${time.format(Date(m.ts))}", 11f, MUTED))
        box.addView(TextView(context).apply {
            text = m.text
            textSize = 15f
            setTextColor(if (m.mine) Color.WHITE else TEXT)
            setPadding(dp(12), dp(8), dp(12), dp(8))
            background = rounded(if (m.mine) ACCENT else Color.WHITE, dp(12).toFloat(),
                if (m.mine) 0 else dp(1))
            maxWidth = (resources.displayMetrics.widthPixels * 0.75f).toInt()
        })
        list.addView(box, LayoutParams(MATCH, WRAP).apply { topMargin = dp(6) })
        scroll.post { scroll.fullScroll(ScrollView.FOCUS_DOWN) }
    }

    fun info(text: String) {
        list.addView(context.text(text, 12f, 0xFF9AA0A6.toInt()).apply { gravity = Gravity.CENTER },
            LayoutParams(MATCH, WRAP).apply { topMargin = dp(6) })
    }

    fun focusInput() = input.requestFocus()
}
