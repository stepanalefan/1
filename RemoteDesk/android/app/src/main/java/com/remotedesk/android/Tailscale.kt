package com.remotedesk.android

import android.util.Base64
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.Inet6Address
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.Socket
import java.net.URL
import java.net.URLEncoder
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

data class TsDevice(
    val name: String, val dns: String, val ip: String, val os: String,
    val online: Boolean, var remoteDesk: Boolean = false,
)

/**
 * Tailscale helpers. Other apps can't query the Tailscale app on Android, so the
 * device list comes from the Tailscale web API with a read-only key the user adds
 * in Settings (API access token, or OAuth client with scope devices:core:read).
 */
object Tailscale {
    const val REMOTE_PORT = 7071
    const val ADMIN_KEYS_URL = "https://login.tailscale.com/admin/settings/keys"
    private const val API = "https://api.tailscale.com/api/v2"

    /** This phone's Tailscale IPv4 address if the Tailscale VPN is on. */
    fun localAddress(): String? {
        try {
            val ifs = NetworkInterface.getNetworkInterfaces() ?: return null
            var v6: String? = null
            for (nif in ifs.toList()) {
                if (!nif.isUp) continue
                for (a in nif.inetAddresses.toList()) {
                    val b = a.address
                    if (a is Inet4Address && (b[0].toInt() and 0xff) == 100 && (b[1].toInt() and 0xc0) == 64)
                        return a.hostAddress
                    if (a is Inet6Address && b.size == 16 && (b[0].toInt() and 0xff) == 0xfd &&
                        (b[1].toInt() and 0xff) == 0x7a && (b[2].toInt() and 0xff) == 0x11 &&
                        (b[3].toInt() and 0xff) == 0x5c)
                        v6 = a.hostAddress?.substringBefore('%')
                }
            }
            return v6
        } catch (_: Exception) {
            return null
        }
    }

    private fun authHeader(credential: String): String {
        val c = credential.trim()
        // OAuth client: "tskey-client-<id>-<secret>" or "<client id>:<client secret>"
        val oauth = when {
            c.startsWith("tskey-client-") ->
                c.removePrefix("tskey-client-").substringBefore('-') to c
            ':' in c && !c.startsWith("tskey-api-") -> c.substringBefore(':') to c.substringAfter(':')
            else -> null
        }
        if (oauth == null) {   // API access token: basic auth with empty password
            val b = Base64.encodeToString("$c:".toByteArray(), Base64.NO_WRAP)
            return "Basic $b"
        }
        val body = "grant_type=client_credentials&client_id=${enc(oauth.first)}&client_secret=${enc(oauth.second)}"
        val conn = URL("$API/oauth/token").openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.doOutput = true
        conn.connectTimeout = 10_000
        conn.readTimeout = 10_000
        conn.setRequestProperty("Content-Type", "application/x-www-form-urlencoded")
        conn.outputStream.use { it.write(body.toByteArray()) }
        val token = JSONObject(read(conn)).getString("access_token")
        return "Bearer $token"
    }

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")

    private fun read(conn: HttpURLConnection): String {
        val code = conn.responseCode
        if (code !in 200..299) {
            val err = conn.errorStream?.bufferedReader()?.readText().orEmpty()
            val msg = try { JSONObject(err).optString("message") } catch (_: Exception) { "" }
            throw IOException(when (code) {
                401 -> "Tailscale rejected the key (expired or wrong?)"
                403 -> "The key is not allowed to read devices${if (msg.isNotEmpty()) ": $msg" else ""}"
                else -> "Tailscale API error $code ${msg.ifEmpty { err.take(120) }}"
            })
        }
        return conn.inputStream.bufferedReader().readText()
    }

    private fun parseTime(s: String): Long? = try {
        SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US).apply { timeZone = TimeZone.getTimeZone("UTC") }
            .parse(s.take(19))?.time
    } catch (_: Exception) { null }

    /** Devices of the tailnet (blocking; call from a background thread). */
    fun devices(credential: String): List<TsDevice> {
        val conn = URL("$API/tailnet/-/devices").openConnection() as HttpURLConnection
        conn.connectTimeout = 10_000
        conn.readTimeout = 15_000
        conn.setRequestProperty("Authorization", authHeader(credential))
        val arr = JSONObject(read(conn)).optJSONArray("devices") ?: return emptyList()
        val mine = localAddress()
        val now = System.currentTimeMillis()
        val out = mutableListOf<TsDevice>()
        for (i in 0 until arr.length()) {
            val d = arr.getJSONObject(i)
            val addrs = d.optJSONArray("addresses") ?: continue
            val ips = (0 until addrs.length()).map { addrs.getString(it) }
            if (ips.isEmpty() || (mine != null && mine in ips)) continue
            val ip = ips.firstOrNull { '.' in it } ?: ips[0]
            val last = parseTime(d.optString("lastSeen"))
            val online = d.optBoolean("connectedToControl", false) ||
                    (last != null && now - last < 5 * 60_000)
            val dns = d.optString("name")
            out += TsDevice(d.optString("hostname").ifEmpty { dns.substringBefore('.') }, dns, ip,
                d.optString("os"), online)
        }
        return out
    }

    /** Marks online devices whose RemoteDesk direct port answers, in parallel. */
    fun probe(devices: List<TsDevice>, timeoutMs: Int = 1500) {
        val online = devices.filter { it.online }
        if (online.isEmpty()) return
        val pool = Executors.newFixedThreadPool(minOf(8, online.size))
        for (d in online) pool.execute {
            d.remoteDesk = try {
                Socket().use { it.connect(InetSocketAddress(d.ip, REMOTE_PORT), timeoutMs); true }
            } catch (_: Exception) { false }
        }
        pool.shutdown()
        pool.awaitTermination(timeoutMs + 2000L, TimeUnit.MILLISECONDS)
    }
}
