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
    private lateinit var tsStatus: TextView
    private lateinit var tsBox: LinearLayout
    @Volatile private var tsLoading = false
    private lateinit var shareStatus: TextView
    private val main = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        setContentView(buildUi())
    }

    override fun onResume() {
        super.onResume()
        refresh()
        refreshTailscale()
        val svc = com.remotedesk.android.host.HostService.instance
        shareStatus.text = when {
            svc?.sessionPeer != null -> "● Remote session active"
            svc?.isSharing == true -> "● Sharing is on" + (svc.deskId?.let { " — ID ${formatId(it)}" } ?: "")
            else -> "Let a computer see and control this phone"
        }
        shareStatus.setTextColor(if (svc?.isSharing == true) 0xFF2EA44F.toInt() else MUTED)
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
        // keep the header text below the status bar and the content above the nav bar / keyboard
        root.onSystemInsets { l, t, r, b ->
            header.setPadding(dp(18) + l, dp(14) + t, dp(8) + r, dp(14))
            root.setPadding(0, 0, 0, b)
        }

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
        c.addView(text("Enter the ID shown in the RemoteDesk app on the other computer, its " +
                "Tailscale name (e.g. office-pc) or its address on your network.", 13f, MUTED), lp(MATCH, WRAP).apply { topMargin = dp(4) })
        address = EditText(this).apply {
            hint = "Desk ID, Tailscale name or address"
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

        // share this device
        val share = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            background = rounded(Color.WHITE, dp(12).toFloat(), dp(1))
            setPadding(dp(16), dp(14), dp(16), dp(14))
            setOnClickListener { startActivity(Intent(this@MainActivity, com.remotedesk.android.host.ShareActivity::class.java)) }
        }
        val st = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        st.addView(text("Share this phone", 17f, bold = true))
        shareStatus = text("Let a computer see and control this phone", 13f, MUTED)
        st.addView(shareStatus)
        share.addView(st, lp(0, WRAP, 1f))
        share.addView(text("›", 26f, MUTED))
        body.addView(share, lp(MATCH, WRAP).apply { topMargin = dp(16) })

        // tailscale
        val t = card()
        val th = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        th.addView(text("Tailscale devices", 17f, bold = true), lp(0, WRAP, 1f))
        th.addView(text("Refresh", 14f, ACCENT).apply {
            setPadding(dp(10), dp(6), 0, dp(6))
            setOnClickListener { refreshTailscale() }
        })
        t.addView(th)
        tsStatus = text("", 13f, MUTED)
        t.addView(tsStatus, lp(MATCH, WRAP).apply { topMargin = dp(4) })
        tsBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        t.addView(tsBox, lp(MATCH, WRAP).apply { topMargin = dp(6) })
        body.addView(t, lp(MATCH, WRAP).apply { topMargin = dp(16) })

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

    private fun refreshTailscale() {
        if (tsLoading) return
        val ip = Tailscale.localAddress()
        val key = prefs.tailscaleKey
        tsBox.removeAllViews()
        if (ip == null) {
            tsStatus.text = "● Tailscale is not active on this phone. Install the Tailscale app, log in " +
                    "with the same account as your computers and turn it on — then connect without a server."
            tsStatus.setTextColor(MUTED)
            return
        }
        tsStatus.setTextColor(0xFF2EA44F.toInt())
        if (key.isEmpty()) {
            tsStatus.text = "● Tailscale is on ($ip). Type a computer's Tailscale name (e.g. office-pc) " +
                    "or 100.x address above — or add a Tailscale API key in Settings to list your devices here."
            return
        }
        tsStatus.text = "● Tailscale is on ($ip). Loading devices…"
        tsLoading = true
        Thread {
            val result = try { Result.success(Tailscale.devices(key).also { Tailscale.probe(it) }) }
            catch (e: Exception) { Result.failure(e) }
            main.post {
                tsLoading = false
                if (isFinishing) return@post
                result.onFailure {
                    tsStatus.setTextColor(0xFFC0392B.toInt())
                    tsStatus.text = "Could not load devices: ${it.message ?: it.javaClass.simpleName}"
                }
                result.onSuccess { list -> showTailscale(ip, list) }
            }
        }.start()
    }

    private fun showTailscale(ip: String, list: List<TsDevice>) {
        val sorted = list.sortedWith(compareBy<TsDevice>({ !it.remoteDesk }, { !it.online }, { it.name.lowercase() }))
        val ready = list.count { it.remoteDesk }
        tsStatus.text = "● Tailscale is on ($ip). ${list.size} devices, ${list.count { it.online }} online, " +
                "$ready running RemoteDesk."
        tsBox.removeAllViews()
        for (d in sorted) {
            val tile = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                background = rounded(Color.WHITE, dp(10).toFloat(), dp(1))
                setPadding(dp(14), dp(10), dp(14), dp(10))
                alpha = if (d.online) 1f else 0.5f
                if (d.online) setOnClickListener { askPasswordAndConnect(d.ip, d.name) }
            }
            val texts = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
            texts.addView(text("${d.name}  ", 16f, bold = true).apply { append(d.os) })
            val (label, color) = when {
                d.remoteDesk -> "● RemoteDesk ready" to 0xFF2EA44F.toInt()
                d.online -> "● online" to 0xFFE0A100.toInt()
                else -> "● offline" to MUTED
            }
            texts.addView(text("${d.ip}   ", 13f, MUTED).apply {
                append(android.text.SpannableString(label).apply {
                    setSpan(android.text.style.ForegroundColorSpan(color), 0, label.length, 0)
                })
            })
            tile.addView(texts, lp(0, WRAP, 1f))
            if (d.online) tile.addView(text("›", 26f, MUTED))
            tsBox.addView(tile, lp(MATCH, WRAP).apply { topMargin = dp(8) })
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

        box.addView(text("Tailscale API key (optional)", 14f, bold = true),
            lp(MATCH, WRAP).apply { topMargin = dp(16) })
        val tsKey = EditText(this).apply {
            setText(prefs.tailscaleKey)
            hint = "tskey-api-… or tskey-client-…"
            isSingleLine = true
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        box.addView(tsKey)
        box.addView(text("Only needed to list your Tailscale devices on the main screen. Create it at " +
                "Tailscale admin console → Settings → Keys (an OAuth client with the read-only " +
                "\"devices:core:read\" scope never expires). Without it, type device names instead.",
            12f, MUTED))
        box.addView(text("Open Tailscale keys page", 13f, ACCENT).apply {
            setPadding(0, dp(6), 0, dp(6))
            setOnClickListener {
                try { startActivity(Intent(Intent.ACTION_VIEW, android.net.Uri.parse(Tailscale.ADMIN_KEYS_URL))) }
                catch (_: Exception) {}
            }
        })

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
                prefs.tailscaleKey = tsKey.text.toString()
                refresh()
                refreshTailscale()
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

    private fun askPasswordAndConnect(target: String, name: String? = null) {
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
                            startSession(target, ch, name)
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

    private fun startSession(target: String, ch: com.remotedesk.android.protocol.SecureChannel, name: String?) {
        prefs.touchRecent(target, name)
        address.setText("")
        Session.channel = ch
        Session.title = prefs.recent().firstOrNull { it.target == target }?.name?.ifEmpty { null } ?: formatId(target)
        startActivity(Intent(this, SessionActivity::class.java))
        Toast.makeText(this, "Connected", Toast.LENGTH_SHORT).show()
    }
}
