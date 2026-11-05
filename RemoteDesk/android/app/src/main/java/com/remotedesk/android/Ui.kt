package com.remotedesk.android

import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.content.res.ColorStateList
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

const val ACCENT = 0xFFE5483E.toInt()
const val ACCENT_DARK = 0xFFC23A31.toInt()
const val TEXT = 0xFF1F2328.toInt()
const val MUTED = 0xFF6B7178.toInt()
const val BORDER = 0xFFE3E5E8.toInt()

fun Context.dp(v: Int): Int = (v * resources.displayMetrics.density + 0.5f).toInt()
fun View.dp(v: Int): Int = context.dp(v)

fun rounded(color: Int, radius: Float, stroke: Int = 0, strokeColor: Int = BORDER) =
    GradientDrawable().apply {
        setColor(color)
        cornerRadius = radius
        if (stroke > 0) setStroke(stroke, strokeColor)
    }

fun Context.text(s: String, sizeSp: Float = 15f, color: Int = TEXT, bold: Boolean = false) =
    TextView(this).apply {
        text = s
        setTextSize(TypedValue.COMPLEX_UNIT_SP, sizeSp)
        setTextColor(color)
        if (bold) typeface = Typeface.DEFAULT_BOLD
    }

fun Context.button(label: String, primary: Boolean = false, onClick: () -> Unit) =
    Button(this).apply {
        text = label
        isAllCaps = false
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
        setTextColor(if (primary) Color.WHITE else TEXT)
        if (primary) typeface = Typeface.DEFAULT_BOLD
        val bg = rounded(if (primary) ACCENT else Color.WHITE, dp(8).toFloat(),
            if (primary) 0 else dp(1), 0xFFD0D4D9.toInt())
        background = RippleDrawable(ColorStateList.valueOf(0x33000000), bg, null)
        stateListAnimator = null
        setPadding(dp(18), dp(10), dp(18), dp(10))
        minHeight = dp(44)
        setOnClickListener { onClick() }
    }

fun Context.card(): LinearLayout = LinearLayout(this).apply {
    orientation = LinearLayout.VERTICAL
    background = rounded(Color.WHITE, dp(12).toFloat(), dp(1))
    setPadding(dp(16), dp(14), dp(16), dp(16))
    elevation = dp(1).toFloat()
}

fun lp(w: Int, h: Int, weight: Float = 0f, gravity: Int = Gravity.NO_GRAVITY) =
    LinearLayout.LayoutParams(w, h, weight).apply { this.gravity = gravity }

const val MATCH = LinearLayout.LayoutParams.MATCH_PARENT
const val WRAP = LinearLayout.LayoutParams.WRAP_CONTENT
