package com.remotedesk.android

import android.content.Context
import com.remotedesk.android.protocol.Connector
import org.json.JSONArray
import org.json.JSONObject

data class Recent(val target: String, val name: String, val time: Long)

class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("remotedesk", Context.MODE_PRIVATE)

    /** RemoteDesk Server address, "host" or "host:port". */
    var server: String
        get() = sp.getString("server", "") ?: ""
        set(v) = sp.edit().putString("server", v.trim()).apply()

    val serverAddress: Pair<String, Int>? get() = Connector.parseHostPort(server, 7070)

    /** true = touchpad (relative pointer), false = direct touch. */
    var touchpad: Boolean
        get() = sp.getBoolean("touchpad", true)
        set(v) = sp.edit().putBoolean("touchpad", v).apply()

    /** 0 = best, 1 = balanced, 2 = fast. */
    var quality: Int
        get() = sp.getInt("quality", 1)
        set(v) = sp.edit().putInt("quality", v).apply()

    /** Tailscale API access token or OAuth client (read-only), optional. */
    var tailscaleKey: String
        get() = sp.getString("ts_key", "") ?: ""
        set(v) = sp.edit().putString("ts_key", v.trim()).apply()

    var showHelp: Boolean
        get() = sp.getBoolean("show_help", true)
        set(v) = sp.edit().putBoolean("show_help", v).apply()

    fun recent(): List<Recent> {
        val arr = try { JSONArray(sp.getString("recent", "[]")) } catch (_: Exception) { JSONArray() }
        return (0 until arr.length()).mapNotNull {
            val o = arr.optJSONObject(it) ?: return@mapNotNull null
            Recent(o.optString("target"), o.optString("name"), o.optLong("time"))
        }.filter { it.target.isNotEmpty() }
    }

    private fun saveRecent(list: List<Recent>) {
        val arr = JSONArray()
        list.take(20).forEach {
            arr.put(JSONObject().put("target", it.target).put("name", it.name).put("time", it.time))
        }
        sp.edit().putString("recent", arr.toString()).apply()
    }

    fun touchRecent(target: String, name: String? = null) {
        val list = recent()
        val old = list.firstOrNull { it.target == target }
        val keep = old?.name?.ifEmpty { null } ?: name ?: ""
        saveRecent(listOf(Recent(target, keep, System.currentTimeMillis())) +
                list.filter { it.target != target })
    }

    fun rename(target: String, name: String) =
        saveRecent(recent().map { if (it.target == target) it.copy(name = name.trim()) else it })

    fun remove(target: String) = saveRecent(recent().filter { it.target != target })

    companion object {
        val QUALITY = listOf(85 to 20, 60 to 15, 35 to 12)   // (jpeg quality, fps)
        val QUALITY_NAMES = arrayOf("Best quality", "Balanced", "Fast (slow networks)")
    }
}

fun formatId(target: String): String {
    val t = target.replace(" ", "")
    return if (Connector.isDeskId(t)) "${t.substring(0, 3)} ${t.substring(3, 6)} ${t.substring(6)}" else target
}
