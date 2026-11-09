package com.remotedesk.android.host

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.remotedesk.android.ACCENT
import com.remotedesk.android.MATCH
import com.remotedesk.android.MUTED
import com.remotedesk.android.Prefs
import com.remotedesk.android.Tailscale
import com.remotedesk.android.WRAP
import com.remotedesk.android.button
import com.remotedesk.android.card
import com.remotedesk.android.dp
import com.remotedesk.android.formatId
import com.remotedesk.android.lp
import com.remotedesk.android.onSystemInsets
import com.remotedesk.android.randomPassword
import com.remotedesk.android.rounded
import com.remotedesk.android.text
import java.net.Inet4Address
import java.net.NetworkInterface

/** "Share this device": lets a computer (or another phone) see and control this one. */
class ShareActivity : Activity(), HostService.Observer {
    private lateinit var prefs: Prefs
    private lateinit var idLabel: TextView
    private lateinit var addrInfo: TextView
    private lateinit var pw: EditText
    private lateinit var status: TextView
    private lateinit var toggle: android.widget.Button
    private lateinit var banner: TextView
    private lateinit var bannerBox: LinearLayout
    private lateinit var permBox: LinearLayout
    private var acceptDialog: AlertDialog? = null
    private lateinit var chatPanel: com.remotedesk.android.ChatView
    private lateinit var chatButton: TextView
    private var shownChat = 0
    /** HostService checks this to decide whether a chat message needs a notification. */
    val chatVisible get() = ::chatPanel.isInitialized && chatPanel.visibility == View.VISIBLE

    companion object { private const val REQ_CAPTURE = 1; private const val REQ_NOTIF = 2 }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        setContentView(buildUi())
    }

    override fun onResume() {
        super.onResume()
        HostService.instance?.observer = this
        refresh()
    }

    override fun onPause() {
        super.onPause()
        HostService.instance?.let { if (it.observer === this) it.observer = null }
    }

    override fun onHostChanged() = refresh()

    // ------------------------------------------------------------------ UI
    private fun buildUi(): View {
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(ACCENT)
        }
        header.addView(text("‹", 30f, Color.WHITE).apply {
            setPadding(dp(14), 0, dp(14), 0)
            setOnClickListener { finish() }
        })
        header.addView(text("Share this device", 20f, Color.WHITE, bold = true), lp(0, WRAP, 1f))
        root.addView(header, lp(MATCH, WRAP))
        root.onSystemInsets { l, t, r, b ->
            header.setPadding(l, dp(12) + t, r + dp(12), dp(12))
            root.setPadding(0, 0, 0, b)
        }
        if (Build.VERSION.SDK_INT < 35) header.setPadding(0, dp(12), dp(12), dp(12))
        chatPanel = com.remotedesk.android.ChatView(this) { text ->
            HostService.instance?.let { svc ->
                if (svc.viewers.isEmpty()) chatPanel.info("Nobody is connected")
                else chatPanel.add(svc.sendChat(text)).also { shownChat = svc.chat.size }
            }
        }.apply { visibility = View.GONE }

        val body = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(16), dp(16), dp(16))
        }
        root.addView(ScrollView(this).apply { addView(body) }, lp(MATCH, 0, 1f))

        bannerBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(0xFFC0392B.toInt(), dp(10).toFloat())
            setPadding(dp(14), dp(12), dp(14), dp(10))
            visibility = View.GONE
        }
        banner = TextView(this).apply { setTextColor(Color.WHITE); textSize = 15f }
        bannerBox.addView(banner)
        val bRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.END }
        chatButton = text("Chat", 15f, Color.WHITE, bold = true).apply {
            setPadding(dp(14), dp(8), dp(14), dp(8)); setOnClickListener { toggleChat() }
        }
        bRow.addView(chatButton)
        bRow.addView(text("End session", 15f, Color.WHITE, bold = true).apply {
            setPadding(dp(14), dp(8), dp(4), dp(8)); setOnClickListener { endSession() }
        })
        bannerBox.addView(bRow)
        body.addView(bannerBox, lp(MATCH, WRAP).apply { bottomMargin = dp(12) })

        // this device
        val c = card()
        c.addView(text("Your address — give it to the person who will connect", 13f, MUTED))
        idLabel = text("—", 28f, bold = true)
        c.addView(idLabel, lp(MATCH, WRAP).apply { topMargin = dp(4) })
        addrInfo = text("", 13f, MUTED)
        c.addView(addrInfo)
        c.addView(text("Password", 13f, MUTED), lp(MATCH, WRAP).apply { topMargin = dp(12) })
        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        pw = EditText(this).apply {
            setText(prefs.hostPassword)
            isSingleLine = true
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setOnFocusChangeListener { _, has -> if (!has) savePassword() }
        }
        row.addView(pw, lp(0, WRAP, 1f))
        row.addView(text("Show", 14f, ACCENT).apply {
            setPadding(dp(10), dp(8), dp(6), dp(8))
            setOnClickListener {
                val hidden = pw.inputType and InputType.TYPE_TEXT_VARIATION_PASSWORD != 0
                pw.inputType = InputType.TYPE_CLASS_TEXT or
                        (if (hidden) InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD else InputType.TYPE_TEXT_VARIATION_PASSWORD)
                text = if (hidden) "Hide" else "Show"
            }
        })
        row.addView(text("New", 14f, ACCENT).apply {
            setPadding(dp(10), dp(8), dp(6), dp(8))
            setOnClickListener { pw.setText(randomPassword()); savePassword() }
        })
        c.addView(row)
        val control = CheckBox(this).apply {
            text = "Allow remote control (otherwise view only)"
            isChecked = prefs.allowControl
            setOnCheckedChangeListener { _, v -> prefs.allowControl = v }
        }
        c.addView(control)
        status = text("", 13f, MUTED)
        c.addView(status, lp(MATCH, WRAP).apply { topMargin = dp(6) })
        toggle = button("Start sharing", primary = true) { toggleSharing() }
        c.addView(toggle, lp(MATCH, WRAP).apply { topMargin = dp(10) })
        body.addView(c, lp(MATCH, WRAP))

        // permissions
        val p = card()
        p.addView(text("Setup", 17f, bold = true))
        permBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        p.addView(permBox)
        body.addView(p, lp(MATCH, WRAP).apply { topMargin = dp(16) })

        body.addView(text("When someone connects, you are asked to allow it, then Android asks to " +
                "start screen recording — choose \"Entire screen\". The phone screen stays on during a " +
                "session. A locked phone shows only the lock screen.", 13f, MUTED),
            lp(MATCH, WRAP).apply { topMargin = dp(14) })
        val frame = android.widget.FrameLayout(this)
        frame.addView(root)
        frame.addView(chatPanel, android.widget.FrameLayout.LayoutParams(MATCH, MATCH).apply { topMargin = dp(64) })
        return frame
    }

    private fun toggleChat() {
        chatPanel.visibility = if (chatVisible) View.GONE else View.VISIBLE
        if (chatVisible) chatPanel.focusInput()
        refresh()
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (chatVisible) toggleChat() else @Suppress("DEPRECATION") super.onBackPressed()
    }

    private fun permRow(ok: Boolean, title: String, detail: String, action: String?, onClick: () -> Unit) {
        val r = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(10), 0, dp(4))
        }
        r.addView(text(if (ok) "✓" else "!", 18f, if (ok) 0xFF2EA44F.toInt() else 0xFFE0A100.toInt(), bold = true),
            lp(dp(28), WRAP))
        val t = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        t.addView(text(title, 15f, bold = true))
        t.addView(text(detail, 12f, MUTED))
        r.addView(t, lp(0, WRAP, 1f))
        if (!ok && action != null) r.addView(text(action, 14f, ACCENT).apply {
            setPadding(dp(10), dp(8), 0, dp(8))
            setOnClickListener { onClick() }
        })
        permBox.addView(r)
    }

    private fun refresh() = runOnUiThread {
        if (isFinishing) return@runOnUiThread
        val svc = HostService.instance
        val sharing = svc?.isSharing == true
        toggle.text = if (sharing) "Stop sharing" else "Start sharing"

        val ts = Tailscale.localAddress()
        val lan = localIPv4()
        val id = svc?.deskId ?: prefs.deskId.ifEmpty { null }
        idLabel.text = when {
            prefs.serverAddress != null && id != null -> formatId(id)
            ts != null -> ts
            lan.isNotEmpty() -> lan.first()
            else -> "—"
        }
        addrInfo.text = buildList {
            if (ts != null && idLabel.text != ts) add("Tailscale: $ts")
            if (lan.isNotEmpty() && idLabel.text != lan.first()) add("Local network: ${lan.joinToString()}")
        }.joinToString("\n")

        status.text = when {
            !sharing -> "● Not sharing"
            else -> "● " + svc!!.statusText.let { if (svc.online && prefs.serverAddress != null) "Online — waiting for connections" else it }
        }
        status.setTextColor(if (sharing && svc?.online == true) 0xFF2EA44F.toInt() else MUTED)

        val viewers = svc?.viewers.orEmpty()
        bannerBox.visibility = if (viewers.isNotEmpty()) View.VISIBLE else View.GONE
        banner.text = "⚠  Connected to this phone: " + viewers.joinToString(", ") {
            it.name + if (it.control) " (control)" else " (view only)"
        }
        // new chat messages
        val chat = svc?.chat?.toList().orEmpty()
        if (chat.size < shownChat) shownChat = 0
        chat.drop(shownChat).forEach { chatPanel.add(it) }
        val fresh = chat.size - shownChat
        shownChat = chat.size
        if (fresh > 0 && !chatVisible) {
            chatPanel.visibility = View.VISIBLE                // a new message opens the chat
        }
        chatButton.text = "Chat"

        permBox.removeAllViews()
        val a11y = InputService.isEnabled(this)
        permRow(a11y, "Remote control",
            if (a11y) "Enabled — taps, swipes and typing work" else "Enable \"RemoteDesk\" under Accessibility so the remote user can tap and type",
            "Enable") { openAccessibility() }
        if (Build.VERSION.SDK_INT >= 33) {
            val n = checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
            permRow(n, "Notifications", if (n) "Allowed" else "Needed to tell you when someone connects", "Allow") {
                requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIF)
            }
        }
        val pm = getSystemService(PowerManager::class.java)
        val bat = pm.isIgnoringBatteryOptimizations(packageName)
        permRow(bat, "Run in background", if (bat) "Battery optimisation is off for RemoteDesk"
            else "Recommended so the phone stays reachable when the screen is off", "Allow") {
            try {
                @Suppress("BatteryLife")
                startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
            } catch (_: Exception) {
                startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            }
        }

        val pending = svc?.pendingPeer
        if (pending != null && acceptDialog == null) showAccept(pending)
        if (pending == null) { acceptDialog?.dismiss(); acceptDialog = null }
    }

    private fun localIPv4(): List<String> = try {
        NetworkInterface.getNetworkInterfaces().toList().filter { it.isUp && !it.isLoopback }
            .flatMap { it.inetAddresses.toList() }
            .filterIsInstance<Inet4Address>()
            .filter { it.isSiteLocalAddress }
            .mapNotNull { it.hostAddress }
    } catch (_: Exception) { emptyList() }

    private fun savePassword() {
        val p = pw.text.toString()
        if (p.length < 4) {
            Toast.makeText(this, "Use at least 4 characters", Toast.LENGTH_SHORT).show()
            pw.setText(prefs.hostPassword)
            return
        }
        prefs.hostPassword = p
        HostService.instance?.setPassword(p)
    }

    private fun toggleSharing() {
        savePassword()
        val svc = HostService.instance
        if (svc?.isSharing == true) {
            svc.stopSharing()
            refresh()
            return
        }
        if (!InputService.isEnabled(this) && prefs.allowControl) {
            AlertDialog.Builder(this).setTitle("Remote control is off")
                .setMessage("The remote user will only see the screen until you enable RemoteDesk under " +
                        "Accessibility. Start sharing anyway?")
                .setPositiveButton("Start") { _, _ -> startService() }
                .setNeutralButton("Enable") { _, _ -> openAccessibility() }
                .setNegativeButton("Cancel", null).show()
            return
        }
        startService()
    }

    private fun startService() {
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIF)
        val i = Intent(this, HostService::class.java).setAction(HostService.ACTION_START)
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(i) else startService(i)
        window.decorView.postDelayed({
            HostService.instance?.observer = this
            refresh()
        }, 300)
    }

    private fun openAccessibility() {
        AlertDialog.Builder(this).setTitle("Enable remote control")
            .setMessage("Android only lets apps perform taps and typing through an Accessibility service.\n\n" +
                    "On the next screen open \"Installed apps\" (or \"Downloaded apps\"), choose RemoteDesk " +
                    "and turn it on.\n\nRemoteDesk only acts on input from a remote user you accepted.")
            .setPositiveButton("Open settings") { _, _ ->
                startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            }.setNegativeButton("Cancel", null).show()
    }

    private fun showAccept(peer: String) {
        val svc = HostService.instance
        val joining = svc?.hasSession == true
        val controller = svc?.controllerName
        val msg = if (joining)
            "$peer entered the correct password and wants to join the session." +
                    (controller?.let { "\n\n$it has control, so $peer can only watch." } ?: "")
        else "$peer entered the correct password and wants to " +
                (if (prefs.allowControl) "see and control" else "see") + " this phone.\n\n" +
                "Next, Android asks to start recording — choose \"Entire screen\"."
        acceptDialog = AlertDialog.Builder(this)
            .setTitle(if (joining) "Allow another viewer?" else "Allow remote access?")
            .setMessage(msg)
            .setCancelable(false)
            .setPositiveButton("Allow") { _, _ ->
                acceptDialog = null
                if (joining) {
                    HostService.instance?.acceptPending()
                    return@setPositiveButton
                }
                val mpm = getSystemService(MediaProjectionManager::class.java)
                @Suppress("DEPRECATION")
                startActivityForResult(mpm.createScreenCaptureIntent(), REQ_CAPTURE)
            }
            .setNegativeButton("Decline") { _, _ ->
                acceptDialog = null
                HostService.instance?.decline()
            }
            .show()
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_CAPTURE) return
        if (resultCode != RESULT_OK || data == null) {
            HostService.instance?.decline()
            return
        }
        val i = Intent(this, HostService::class.java).setAction(HostService.ACTION_PROJECTION)
            .putExtra(HostService.EXTRA_CODE, resultCode).putExtra(HostService.EXTRA_DATA, data)
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(i) else startService(i)
    }

    @Deprecated("Deprecated in Java")
    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) =
        refresh()

    private fun endSession() {
        AlertDialog.Builder(this).setTitle("End the session for everyone?")
            .setPositiveButton("End") { _, _ ->
                startService(Intent(this, HostService::class.java).setAction(HostService.ACTION_END_SESSION))
            }.setNegativeButton("Cancel", null).show()
    }
}
