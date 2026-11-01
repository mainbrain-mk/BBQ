package io.github.mainbrain_mk.bbq

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch

/** Haelt die Verbindung zum Pi oder direkt zum Thermometer, auch mit ausgeschaltetem Bildschirm, zeigt die Werte als dauerhafte
 *  Benachrichtigung und schlaegt Alarm, sobald ein Fuehler sein Ziel erreicht. "Beenden" trennt. */
class BbqService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var watch: Job? = null
    private lateinit var nm: NotificationManager
    private var shown = ""
    private val reached = BooleanArray(3) // [1], [2]: Ziel erreicht und schon gemeldet
    private var wake: PowerManager.WakeLock? = null
    private var wifi: WifiManager.WifiLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            Bbq.stop()
            stopSelf()
            return START_NOT_STICKY
        }
        Bbq.init(applicationContext)
        nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CH_STATUS, getString(R.string.channel_status), NotificationManager.IMPORTANCE_LOW))
        nm.createNotificationChannel(NotificationChannel(CH_ALARM, getString(R.string.channel_alarm), NotificationManager.IMPORTANCE_HIGH)
            .apply { enableVibration(true) })
        try {
            ServiceCompat.startForeground(this, ID_STATUS, status(getString(R.string.connecting)), ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } catch (_: IllegalStateException) {
            // ab Android 12: nach dem Beenden der App startet das System den Dienst neu (START_STICKY), verbietet ihm
            // aus dem Hintergrund aber den Vordergrund (ForegroundServiceStartNotAllowedException). Dann still enden,
            // beim naechsten Oeffnen der App verbindet sie wieder.
            stopSelf()
            return START_NOT_STICKY
        }
        if (!Bbq.paired) {
            stopSelf()
            return START_NOT_STICKY
        }
        // ponytail: CPU (und ueber den Pi auch WLAN) bleiben wach, solange der Dienst laeuft (kostet Akku, dafuer
        // kein Messwert-Loch im Standby); gehen mit "Beenden" bzw. dem Ende des Dienstes wieder frei.
        if (wake == null) {
            wake = getSystemService(PowerManager::class.java).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "bbq:verbindung")
                .apply { acquire() }
            @Suppress("DEPRECATION")
            if (!Bbq.direct) wifi = getSystemService(WifiManager::class.java)
                .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "bbq:wlan").apply { acquire() }
        }
        Bbq.start()
        if (watch == null) watch = scope.launch { Bbq.state.collect(::onState) }
        return START_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        wake?.release()
        wifi?.release()
        super.onDestroy()
    }

    private fun onState(s: UiState) {
        if (!s.running && Bbq.paired && s.page == Page.SETUP) { // auf dem Pi entkoppelt oder anderes Zertifikat
            stopSelf()
            return
        }
        val r = s.reading
        val fresh = r != null && SystemClock.elapsedRealtime() - s.readingAt < STALE_MS
        val text = if (fresh) getString(R.string.notify_readings, temp(r!!.p1), r.t1, temp(r.p2), r.t2)
        else s.status.ifEmpty { getString(R.string.waiting) }
        if (text != shown) {
            shown = text
            nm.notify(ID_STATUS, status(text))
        }
        if (!fresh) return
        for (i in 1..2) {
            val p = r!!.probe(i)
            val t = r.target(i)
            if (p >= NO_PROBE || t <= 0) continue
            if (!reached[i] && p >= t) {
                reached[i] = true
                nm.notify(ID_ALARM + i, NotificationCompat.Builder(this, CH_ALARM)
                    .setSmallIcon(R.drawable.ic_stat).setContentTitle(getString(R.string.alarm_title, i, p))
                    .setContentText(getString(R.string.alarm_text, t)).setCategory(NotificationCompat.CATEGORY_ALARM)
                    .setPriority(NotificationCompat.PRIORITY_HIGH).setContentIntent(openApp()).setAutoCancel(true).build())
            } else if (reached[i] && p < t - 2) {
                reached[i] = false // 2 °C Abstand: an der Schwelle nicht staendig neu Alarm geben
            }
        }
    }

    private fun temp(v: Int) = if (v >= NO_PROBE) "--" else "$v °C"

    private fun status(text: String): Notification {
        val stop = PendingIntent.getService(this, 1, Intent(this, BbqService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(this, CH_STATUS).setSmallIcon(R.drawable.ic_stat).setContentTitle(getString(R.string.app_name))
            .setContentText(text).setOngoing(true).setOnlyAlertOnce(true).setSilent(true).setContentIntent(openApp())
            .addAction(0, getString(R.string.stop), stop).build()
    }

    private fun openApp() = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java)
        .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE)

    companion object {
        const val ACTION_STOP = "io.github.mainbrain_mk.bbq.STOP"
        const val CH_STATUS = "status"
        const val CH_ALARM = "alarm"
        const val ID_STATUS = 1
        const val ID_ALARM = 10
    }
}
