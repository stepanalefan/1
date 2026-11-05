package com.remotedesk.android

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.text.format.DateFormat
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.CheckBox
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.remotedesk.android.protocol.AuthException
import com.remotedesk.android.protocol.Connector
import java.net.Socket
import java.util.Date

class MainActivity : Activity() {
    private lateinit var prefs: Prefs
    private lateinit var address: EditText
    private lateinit var serverLine: TextView
    private lateinit var recentBox: LinearLayout
    private val main = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        setContentView(buildUi())
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    // ------------------------------------------------------------------ UI
    private fun buildUi(): View {
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }

        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(ACCENT)
            setPadding(dp(18), dp(14), dp(8), dp(14))
        }
        header.addView(text("RemoteDesk", 22f, Color.WHITE, bold = true), lp(0, WRAP, 1f))
        header.addView(TextView(this).apply {
            text = "Settings"
            setTextColor(Color.WHITE)
            textSize = 15f
            setPadding(dp(14), dp(8), dp(14), dp(8))
            background = rounded(0x30FFFFFF, dp(8).toFloat())
            setOnClickListener { showSettings() }
        })
        root.addView(header, lp(MATCH, WRAP))

        val scroll = ScrollView(this)
        val body = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(16), dp(16), dp(16))
        }
        scroll.addView(body)
        root.addView(scroll, lp(MATCH, 0, 1f))

        // connect card
        val c = card()
        c.addView(text("Remote Desk", 17f, bold = true))
        c.addView(text("Enter the ID shown in the RemoteDesk app on the other computer, " +
                "or its address on your network.", 13f, MUTED), lp(MATCH, WRAP).apply { topMargin = dp(4) })
        address = EditText(this).apply {
            hint = "Desk ID, e.g. 123 456 789"
            textSize = 20f
            isSingleLine = true
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS or
                    InputType.TYPE_TEXT_VARIATION_URI
            imeOptions = EditorInfo.IME_ACTION_GO
            background = rounded(Color.WHITE, dp(8).toFloat(), dp(1), 0xFFD0D4D9.toInt())
            setPadding(dp(12), dp(10), dp(12), dp(10))
            setOnEditorActionListener { _, id, ev ->
                if (id == EditorInfo.IME_ACTION_GO ||
                    (ev?.keyCode == KeyEvent.KEYCODE_ENTER && ev.action == KeyEvent.ACTION_DOWN)) {
                    connectClicked(); true
                } else false
            }
        }
        c.addView(address, lp(MATCH, WRAP).apply { topMargin = dp(12) })
        c.addView(button("Connect", primary = true) { connectClicked() },
            lp(MATCH, WRAP).apply { topMargin = dp(12) })
        serverLine = text("", 13f, MUTED)
        c.addView(serverLine, lp(MATCH, WRAP).apply { topMargin = dp(10) })
        body.addView(c, lp(MATCH, WRAP))

        // recent sessions
        body.addView(text("Recent sessions", 17f, bold = true),
            lp(MATCH, WRAP).apply { topMargin = dp(22); bottomMargin = dp(8) })
        recentBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        body.addView(recentBox, lp(MATCH, WRAP))
        return root
    }

    private fun refresh() {
        val srv = prefs.server
        serverLine.text = if (srv.isEmpty()) "● No server set — tap Settings to add your RemoteDesk Server. " +
                "Direct addresses (e.g. 192.168.1.20) still work."
        else "● Server: $srv"
        serverLine.setTextColor(if (srv.isEmpty()) MUTED else 0xFF2EA44F.toInt())

        recentBox.removeAllViews()
        val recent = prefs.recent()
        if (recent.isEmpty()) {
            recentBox.addView(text("Desks you connect to will appear here.", 14f, MUTED))
            return
        }
        for (r in recent) {
            val tile = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                background = rounded(Color.WHITE, dp(10).toFloat(), dp(1))
                setPadding(dp(14), dp(12), dp(14), dp(12))
                isClickable = true
                setOnClickListener { askPasswordAndConnect(r.target) }
                setOnLongClickListener { tileMenu(r); true }
            }
            val badge = TextView(this).apply {
                text = (r.name.ifEmpty { "#" }).take(1).uppercase()
                setTextColor(Color.WHITE)
                textSize = 18f
                gravity = Gravity.CENTER
                background = rounded(ACCENT, dp(8).toFloat())
            }
            tile.addView(badge, lp(dp(42), dp(42)))
            val texts = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
            texts.addView(text(r.name.ifEmpty { formatId(r.target) }, 16f, bold = true))
            val whenStr = DateFormat.getMediumDateFormat(this).format(Date(r.time)) + " " +
                    DateFormat.getTimeFormat(this).format(Date(r.time))
            texts.addView(text(if (r.name.isEmpty()) whenStr else "${formatId(r.target)} · $whenStr", 13f, MUTED))
            tile.addView(texts, lp(0, WRAP, 1f).apply { leftMargin = dp(12) })
            tile.addView(text("›", 26f, MUTED))
            recentBox.addView(tile, lp(MATCH, WRAP).apply { bottomMargin = dp(8) })
        }
    }

    private fun tileMenu(r: Recent) {
        AlertDialog.Builder(this)
            .setTitle(r.name.ifEmpty { formatId(r.target) })
            .setItems(arrayOf("Connect", "Rename", "Remove")) { _, which ->
                when (which) {
                    0 -> askPasswordAndConnect(r.target)
                    1 -> {
                        val e = EditText(this).apply { setText(r.name); hint = "e.g. Office PC" }
                        AlertDialog.Builder(this).setTitle("Name for this desk").setView(padded(e))
                            .setPositiveButton("Save") { _, _ -> prefs.rename(r.target, e.text.toString()); refresh() }
                            .setNegativeButton("Cancel", null).show()
                    }
                    2 -> { prefs.remove(r.target); refresh() }
                }
            }.show()
    }

    private fun padded(v: View) = FrameLayout(this).apply {
        setPadding(dp(20), dp(8), dp(20), 0)
        addView(v)
    }

    // ------------------------------------------------------------ settings
    private fun showSettings() {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(8), dp(20), 0)
        }
        box.addView(text("Server", 14f, bold = true))
        val server = EditText(this).apply {
            setText(prefs.server)
            hint = "e.g. 203.0.113.10:7070"
            isSingleLine = true
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
        }
        box.addView(server)
        box.addView(text("Address of the computer running RemoteDesk Server (port 7070 if omitted).",
            12f, MUTED))

        box.addView(text("Mouse mode", 14f, bold = true), lp(MATCH, WRAP).apply { topMargin = dp(16) })
        val group = RadioGroup(this)
        val tp = RadioButton(this).apply { text = "Touchpad — move a pointer like on a laptop"; id = View.generateViewId() }
        val dt = RadioButton(this).apply { text = "Direct touch — tap where you want to click"; id = View.generateViewId() }
        group.addView(tp); group.addView(dt)
        group.check(if (prefs.touchpad) tp.id else dt.id)
        box.addView(group)

        box.addView(text("Default quality", 14f, bold = true), lp(MATCH, WRAP).apply { topMargin = dp(16) })
        val qGroup = RadioGroup(this)
        val qButtons = Prefs.QUALITY_NAMES.map { n ->
            RadioButton(this).apply { text = n; id = View.generateViewId() }.also { qGroup.addView(it) }
        }
        qGroup.check(qButtons[prefs.quality].id)
        box.addView(qGroup)

        val help = CheckBox(this).apply { text = "Show gesture help when a session starts"; isChecked = prefs.showHelp }
        box.addView(help, lp(MATCH, WRAP).apply { topMargin = dp(12) })

        AlertDialog.Builder(this)
            .setTitle("Settings")
            .setView(ScrollView(this).apply { addView(box) })
            .setPositiveButton("Save") { _, _ ->
                prefs.server = server.text.toString()
                prefs.touchpad = group.checkedRadioButtonId == tp.id
                prefs.quality = qButtons.indexOfFirst { it.id == qGroup.checkedRadioButtonId }.coerceAtLeast(0)
                prefs.showHelp = help.isChecked
                refresh()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // ------------------------------------------------------------- connect
    private fun connectClicked() {
        var t = address.text.toString().trim()
        if (t.isEmpty()) return
        if (Connector.isDeskId(t)) t = t.replace(" ", "")
        askPasswordAndConnect(t)
    }

    private fun askPasswordAndConnect(target: String) {
        if (Connector.isDeskId(target) && prefs.serverAddress == null) {
            AlertDialog.Builder(this)
                .setTitle("No server configured")
                .setMessage("Connecting by ID needs your RemoteDesk Server. Open Settings and enter its " +
                        "address, or connect directly by IP address on the same network.")
                .setPositiveButton("Settings") { _, _ -> showSettings() }
                .setNegativeButton("Cancel", null).show()
            return
        }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(8), dp(20), 0)
        }
        val pw = EditText(this).apply {
            hint = "Password of the remote desk"
            isSingleLine = true
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        box.addView(pw)
        val status = text("", 13f, MUTED)
        val progress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            isIndeterminate = true
            visibility = View.GONE
        }
        box.addView(status, lp(MATCH, WRAP).apply { topMargin = dp(8) })
        box.addView(progress, lp(MATCH, WRAP))

        val dlg = AlertDialog.Builder(this)
            .setTitle("Connect to ${formatId(target)}")
            .setView(box)
            .setPositiveButton("Connect", null)
            .setNegativeButton("Cancel", null)
            .create()
        var socket: Socket? = null
        dlg.setOnDismissListener { socket?.let { s -> Thread { try { s.close() } catch (_: Exception) {} }.start() } }
        dlg.setOnShowListener {
            val go = dlg.getButton(AlertDialog.BUTTON_POSITIVE)
            pw.requestFocus()
            dlg.window?.setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_STATE_VISIBLE)
            fun start() {
                val password = pw.text.toString()
                if (password.isEmpty()) return
                go.isEnabled = false
                pw.isEnabled = false
                progress.visibility = View.VISIBLE
                status.setTextColor(MUTED)
                status.text = "Connecting… waiting for the remote user to accept."
                val s = Socket()
                socket = s
                val server = prefs.serverAddress
                Thread {
                    try {
                        val ch = Connector.open(s, target, password, server)
                        main.post {
                            if (!dlg.isShowing) { Thread { ch.close() }.start(); return@post }
                            socket = null           // keep it open
                            dlg.dismiss()
                            startSession(target, ch)
                        }
                    } catch (e: Exception) {
                        try { s.close() } catch (_: Exception) {}
                        val msg = when {
                            e is AuthException -> "Wrong password."
                            e is java.net.SocketTimeoutException -> "No answer — the remote user did not accept in time."
                            e is java.io.EOFException || e.message?.contains("closed") == true ->
                                "The remote user declined, or the connection was closed."
                            e is java.net.UnknownHostException -> "Unknown address: ${e.message}"
                            e is java.net.ConnectException -> "Cannot reach ${if (Connector.isDeskId(target)) "the server" else target}. " +
                                    "Check the address and that the other side is running."
                            else -> e.message ?: e.javaClass.simpleName
                        }
                        main.post {
                            if (!dlg.isShowing) return@post
                            progress.visibility = View.GONE
                            go.isEnabled = true
                            pw.isEnabled = true
                            pw.selectAll()
                            status.setTextColor(0xFFC0392B.toInt())
                            status.text = msg
                        }
                    }
                }.start()
            }
            go.setOnClickListener { start() }
            pw.setOnEditorActionListener { _, _, _ -> start(); true }
        }
        dlg.show()
    }

    private fun startSession(target: String, ch: com.remotedesk.android.protocol.SecureChannel) {
        prefs.touchRecent(target)
        address.setText("")
        Session.channel = ch
        Session.title = prefs.recent().firstOrNull { it.target == target }?.name?.ifEmpty { null } ?: formatId(target)
        startActivity(Intent(this, SessionActivity::class.java))
        Toast.makeText(this, "Connected", Toast.LENGTH_SHORT).show()
    }
}
