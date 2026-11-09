package com.remotedesk.android.host

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import com.remotedesk.android.Prefs
import com.remotedesk.android.formatId
import com.remotedesk.android.protocol.DeskHost
import com.remotedesk.android.protocol.Msg
import com.remotedesk.android.protocol.SecureChannel

/**
 * Keeps this phone reachable as a desk while sharing is on (foreground service),
 * holds an incoming connection until the user accepts it, then runs the session.
 */
class HostService : Service(), DeskHost.Listener {

    companion object {
        @Volatile var instance: HostService? = null
            private set
        const val ACTION_START = "com.remotedesk.android.START"
        const val ACTION_STOP = "com.remotedesk.android.STOP"
        const val ACTION_PROJECTION = "com.remotedesk.android.PROJECTION"
        const val ACTION_DECLINE = "com.remotedesk.android.DECLINE"
        const val ACTION_END_SESSION = "com.remotedesk.android.END_SESSION"
        const val EXTRA_CODE = "code"
        const val EXTRA_DATA = "data"
        private const val CH_STATUS = "sharing"
        private const val CH_ALERT = "incoming"
        private const val NOTIF_STATUS = 1
        private const val NOTIF_INCOMING = 2
        private const val NOTIF_MESSAGE = 3
        private const val ACCEPT_TIMEOUT_MS = 60_000L
    }

    /** UI (ShareActivity) observes state changes through this. */
    fun interface Observer { fun onHostChanged() }
    @Volatile var observer: Observer? = null

    @Volatile var deskId: String? = null; private set
    @Volatile var statusText = "Starting…"; private set
    @Volatile var online = false; private set
    @Volatile var pendingPeer: String? = null; private set
    @Volatile var sessionPeer: String? = null; private set

    private lateinit var prefs: Prefs
    private val main = Handler(Looper.getMainLooper())
    private var desk: DeskHost? = null
    private var pending: SecureChannel? = null
    private var streamer: ScreenStreamer? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        prefs = Prefs(this)
        val nm = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(NotificationChannel(CH_STATUS, "Sharing status",
                NotificationManager.IMPORTANCE_LOW))
            nm.createNotificationChannel(NotificationChannel(CH_ALERT, "Incoming connections",
                NotificationManager.IMPORTANCE_HIGH))
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> { stopSharing(); return START_NOT_STICKY }
            ACTION_DECLINE -> decline()
            ACTION_END_SESSION -> streamer?.stop()
            ACTION_PROJECTION -> {
                val code = intent.getIntExtra(EXTRA_CODE, 0)
                @Suppress("DEPRECATION")
                val data: Intent? = if (Build.VERSION.SDK_INT >= 33)
                    intent.getParcelableExtra(EXTRA_DATA, Intent::class.java) else intent.getParcelableExtra(EXTRA_DATA)
                startSession(code, data)
            }
            else -> startSharing()
        }
        return START_NOT_STICKY
    }

    // ------------------------------------------------------------ sharing
    private fun startSharing() {
        goForeground(projection = false)
        if (desk != null) return
        deskId = prefs.deskId.ifEmpty { null }
        desk = DeskHost(prefs.hostPassword, this, directPort = 7071, server = prefs.serverAddress,
            deskId = deskId, machineKey = prefs.machineKey, deviceName = deviceName()).also { it.start() }
        statusText = if (prefs.serverAddress == null) "Sharing on — no server set (Tailscale / local network only)"
        else "Connecting to the server…"
        online = prefs.serverAddress == null
        changed()
    }

    fun stopSharing() {
        streamer?.stop()
        decline()
        desk?.stop()
        desk = null
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
        instance = null
        changed()
    }

    fun setPassword(pw: String) { desk?.password = pw }

    /** The name the user gave the phone (Settings → About), else the model. */
    private fun deviceName(): String {
        val n = try {
            android.provider.Settings.Global.getString(contentResolver, android.provider.Settings.Global.DEVICE_NAME)
        } catch (_: Exception) { null }
        return n?.takeIf { it.isNotBlank() } ?: "${Build.MANUFACTURER} ${Build.MODEL}".trim()
    }

    private fun goForeground(projection: Boolean) {
        val n = statusNotification()
        if (Build.VERSION.SDK_INT >= 34) {
            var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
            if (projection) type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
            startForeground(NOTIF_STATUS, n, type)
        } else if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_STATUS, n,
                if (projection) ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION else 0)
        } else {
            startForeground(NOTIF_STATUS, n)
        }
    }

    private fun pi(action: String, req: Int): PendingIntent =
        PendingIntent.getService(this, req, Intent(this, HostService::class.java).setAction(action),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    private fun openUi(req: Int): PendingIntent =
        PendingIntent.getActivity(this, req, Intent(this, ShareActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    private fun builder(channel: String): Notification.Builder =
        if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, channel)
        else @Suppress("DEPRECATION") Notification.Builder(this)

    private fun statusNotification(): Notification {
        val peer = sessionPeer
        val title = if (peer != null) "Remote session active" else "Sharing this device"
        val text = when {
            peer != null -> "$peer can see${if (prefs.allowControl) " and control" else ""} this screen"
            deskId != null -> "Desk ID ${formatId(deskId!!)}"
            else -> statusText
        }
        return builder(CH_STATUS)
            .setSmallIcon(android.R.drawable.ic_menu_share)
            .setContentTitle(title)
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(openUi(10))
            .addAction(Notification.Action.Builder(null,
                if (peer != null) "End session" else "Stop sharing",
                pi(if (peer != null) ACTION_END_SESSION else ACTION_STOP, 11)).build())
            .build()
    }

    private fun updateStatusNotification() {
        if (desk == null) return
        getSystemService(NotificationManager::class.java).notify(NOTIF_STATUS, statusNotification())
    }

    private fun changed() = main.post {
        updateStatusNotification()
        observer?.onHostChanged()
    }

    // ------------------------------------------------------- DeskHost callbacks (network threads)
    override fun onId(id: String) {
        deskId = id
        main.post { prefs.deskId = id }
        changed()
    }

    override fun onStatus(text: String, online: Boolean) {
        statusText = text
        this.online = online
        changed()
    }

    override fun onIncoming(channel: SecureChannel, peer: String) {
        main.post {
            if (streamer != null || pending != null) {
                Thread { channel.close() }.start()
                return@post
            }
            pending = channel
            pendingPeer = peer
            val n = builder(CH_ALERT)
                .setSmallIcon(android.R.drawable.ic_dialog_alert)
                .setContentTitle("$peer wants to connect")
                .setContentText("Tap to allow or decline")
                .setCategory(Notification.CATEGORY_CALL)
                .setPriority(Notification.PRIORITY_HIGH)
                .setAutoCancel(true)
                .setContentIntent(openUi(20))
                .addAction(Notification.Action.Builder(null, "Decline", pi(ACTION_DECLINE, 21)).build())
                .build()
            getSystemService(NotificationManager::class.java).notify(NOTIF_INCOMING, n)
            main.postDelayed({ if (pending === channel) decline() }, ACCEPT_TIMEOUT_MS)
            changed()
        }
    }

    // ------------------------------------------------------------ session
    fun decline() {
        val ch = pending ?: return
        pending = null
        pendingPeer = null
        Thread { ch.close() }.start()
        getSystemService(NotificationManager::class.java).cancel(NOTIF_INCOMING)
        changed()
    }

    private fun startSession(code: Int, data: Intent?) {
        val ch = pending
        val peer = pendingPeer
        pending = null
        pendingPeer = null
        getSystemService(NotificationManager::class.java).cancel(NOTIF_INCOMING)
        if (ch == null || data == null || ch.closed) {
            if (ch != null) Thread { ch.close() }.start()
            changed()
            return
        }
        sessionPeer = peer
        try {
            goForeground(projection = true)       // must come before getMediaProjection (Android 14)
            val mpm = getSystemService(MediaProjectionManager::class.java)
            val projection = mpm.getMediaProjection(code, data) ?: throw IllegalStateException("no projection")
            val s = ScreenStreamer(this, projection, ch, ::onSessionMessage) { main.post { sessionEnded() } }
            streamer = s
            s.start()
            acquireWakeLock()
        } catch (e: Exception) {
            Thread { ch.close() }.start()
            sessionEnded()
            statusText = "Could not start screen capture: ${e.message}"
        }
        changed()
    }

    private fun sessionEnded() {
        streamer = null
        sessionPeer = null
        releaseWakeLock()
        if (desk != null) goForeground(projection = false)
        changed()
    }

    private fun onSessionMessage(type: Int, data: ByteArray) {
        val control = prefs.allowControl
        when (type) {
            Msg.MOUSE -> if (control) InputService.instance?.mouse(data)
            Msg.KEY -> if (control) InputService.instance?.key(data)
            Msg.CLIPBOARD -> main.post {
                getSystemService(ClipboardManager::class.java)
                    .setPrimaryClip(ClipData.newPlainText("RemoteDesk", String(data, Charsets.UTF_8)))
            }
            Msg.CHAT -> main.post {
                val n = builder(CH_ALERT)
                    .setSmallIcon(android.R.drawable.ic_dialog_email)
                    .setContentTitle("Message from the remote user")
                    .setContentText(String(data, Charsets.UTF_8))
                    .setStyle(Notification.BigTextStyle().bigText(String(data, Charsets.UTF_8)))
                    .setAutoCancel(true)
                    .build()
                getSystemService(NotificationManager::class.java).notify(NOTIF_MESSAGE, n)
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun acquireWakeLock() {
        val pm = getSystemService(PowerManager::class.java)
        // keep the screen on (and turn it on) while someone is watching it
        wakeLock = pm.newWakeLock(PowerManager.SCREEN_DIM_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP,
            "RemoteDesk:session").apply { acquire(4 * 60 * 60 * 1000L) }
    }

    private fun releaseWakeLock() {
        try { wakeLock?.takeIf { it.isHeld }?.release() } catch (_: Exception) {}
        wakeLock = null
    }

    override fun onDestroy() {
        streamer?.stop()
        desk?.stop()
        releaseWakeLock()
        if (instance === this) instance = null
        super.onDestroy()
    }

    val isSharing get() = desk != null
    val hasSession get() = streamer != null
}
