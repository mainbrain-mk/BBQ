package io.github.mainbrain_mk.bbq

import android.content.Context
import android.content.SharedPreferences
import android.os.Build
import android.os.SystemClock
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.update
import java.io.IOException
import java.net.InetSocketAddress
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.concurrent.Executors
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.X509TrustManager

enum class Page { SETUP, CARDS, CHART }

data class UiState(
    val page: Page = Page.SETUP,
    val view: Page = Page.CARDS, // Karten oder Verlauf: dorthin nach der Anmeldung
    val status: String = "",
    val address: String = "",
    val reading: Reading? = null,
    val readingAt: Long = 0, // SystemClock.elapsedRealtime() der letzten Messung
    val codeAsked: Boolean = false,
    val piId: String = "", // Pi-Kennung: erste 8 Zeichen des Zertifikat-Fingerabdrucks
    val historySize: Int = 0, // waechst mit jeder Messung -> Verlauf neu zeichnen
    val running: Boolean = false, // Verbindung soll stehen (Dienst laeuft)
    val found: Map<String, String> = emptyMap(), // beim Suchen gefundene Thermometer: Adresse -> "Name · RSSI"
    val scanning: Boolean = false,
    val wrongCodes: Int = 0, // zaehlt falsche Codes: das Eingabefeld leert sich dann
)

/** Verbindung zum Pi oder direkt zum Thermometer (Ble) und Zustand der App, geteilt von Oberflaeche und Dienst.
 *  Pi wie bbq_client.py: TLS, Koppeln per Code, danach nur noch mit genau diesem Pi (Zertifikat gepinnt). */
object Bbq {
    val state = MutableStateFlow(UiState())
    private val history = ArrayList<Sample>() // seit dem Start der App
    private val out = Executors.newSingleThreadExecutor() // Netz nie auf dem UI-Thread
    private lateinit var prefs: SharedPreferences
    private lateinit var ctx: Context
    @Volatile private var generation = 0 // jede neue Verbindung zaehlt hoch, alte Threads geben dann auf
    @Volatile private var socket: SSLSocket? = null
    @Volatile private var mode = "auth" // "auth" (Token) oder "pair" (Code)
    @Volatile private var authed = false
    @Volatile private var note = "" // Grund, warum es zur Kopplung zurueckging
    @Volatile private var seenFp = ""
    @Volatile private var viaBle = false // laufende Verbindung geht direkt zum Thermometer
    @Volatile var choosing = false // "Verbindung" gedrueckt: in der Einrichtung bleiben, nicht von selbst verbinden
        private set
    private var host: Host? = null
    private var token = ""
    private var pin = ""

    fun init(ctx: Context) {
        if (::prefs.isInitialized) return
        this.ctx = ctx
        prefs = ctx.getSharedPreferences("bbq", Context.MODE_PRIVATE) // App-eigen, andere Apps lesen das nicht
        host = parseHost(prefs.getString("host", "") ?: "")
        token = prefs.getString("token", "") ?: ""
        pin = prefs.getString("pin", "") ?: ""
        state.update { it.copy(address = host?.toString() ?: "") }
    }

    /** Direkt zum Thermometer statt ueber den Pi (gewaehlt in der Einrichtung). */
    val direct get() = prefs.getBoolean("direct", false)
    private val mac get() = prefs.getString("mac", "") ?: ""

    val paired get() = if (direct) mac.isNotEmpty() else host != null && token.isNotEmpty()

    fun historySnapshot(): List<Sample> = synchronized(history) { ArrayList(history) }

    /** Gekoppelt: Verbindung aufbauen und halten (ruft der Dienst beim Start). */
    fun start() {
        if (!paired || state.value.running) return
        mode = "auth"
        state.update { it.copy(page = it.view, running = true) }
        connect(direct)
    }

    fun stop() {
        generation++
        close()
        authed = false
        state.update { it.copy(running = false) }
    }

    /** Knopf "Verbindung": trennen und zur Einrichtung, um Pi oder Thermometer zu wechseln. */
    fun change() {
        stop()
        choosing = true
        state.update { it.copy(page = Page.SETUP, status = "") }
    }

    /** Knopf "Koppeln": verbinden und um einen Code bitten, den der Pi auf seinem Display zeigt. */
    fun pair(address: String) {
        val h = parseHost(address)
        if (h == null) {
            state.update { it.copy(status = ctx.getString(R.string.bad_address)) }
            return
        }
        if (h == host && token.isNotEmpty()) { // schon gekoppelt, z. B. zurueck vom Direktbetrieb: kein neuer Code
            prefs.edit().putBoolean("direct", false).apply()
            stop()
            start()
            return
        }
        host = h
        mode = "pair"
        state.update { it.copy(address = h.toString(), codeAsked = false, page = Page.SETUP) }
        connect(false)
    }

    /** Knopf "Thermometer suchen" (Rechte sind dann erteilt). */
    fun scan() {
        state.update { it.copy(found = emptyMap(), scanning = true, status = "") }
        val ok = Ble.scan(ctx, { addr, name, rssi -> state.update { it.copy(found = it.found + (addr to "$name · $rssi dBm")) } }) {
            state.update {
                it.copy(scanning = false, status = if (it.found.isNotEmpty()) "" else
                    ctx.getString(R.string.none_found))
            }
        }
        if (!ok) state.update { it.copy(scanning = false, status = ctx.getString(R.string.bt_off)) }
    }

    /** Gefundenes Thermometer angetippt: ab jetzt direkt mit ihm verbinden. */
    fun useThermometer(address: String) {
        Ble.stopScan()
        prefs.edit().putString("mac", address).putBoolean("direct", true).apply()
        stop()
        state.update { it.copy(found = emptyMap()) }
        start()
    }

    fun sendCode(code: String) {
        if (code.length == 6) write("code $code")
    }

    fun setTarget(probe: Int, t: Int) {
        if (t !in 1..249) return
        if (viaBle) Ble.write(targetCommand(probe, t)) else if (authed) write("p$probe $t")
    }

    fun toggleView() = state.update { s ->
        val v = if (s.view == Page.CARDS) Page.CHART else Page.CARDS
        s.copy(view = v, page = if (s.page == Page.SETUP) s.page else v)
    }

    private fun connect(ble: Boolean) {
        val gen = ++generation
        close()
        authed = false
        note = ""
        choosing = false
        viaBle = ble
        if (ble) Ble.connect(ctx, mac, ::onReading) { msg -> state.update { it.copy(status = msg) } }
        else Thread({ run(gen) }, "bbq-net").apply { isDaemon = true }.start()
    }

    private fun close() {
        Ble.close()
        val s = socket ?: return
        socket = null
        out.execute { // TLS schickt beim Schliessen noch etwas: nie auf dem UI-Thread ("Verbindung", "Beenden")
            try {
                s.close()
            } catch (_: IOException) {
            }
        }
    }

    private fun run(gen: Int) {
        while (gen == generation) {
            val h = host ?: return
            state.update { it.copy(status = ctx.getString(R.string.connecting_to, h.toString())) }
            try {
                val tls = SSLContext.getInstance("TLS").apply { init(null, arrayOf(trust), null) }
                val s = tls.socketFactory.createSocket() as SSLSocket
                socket = s
                s.connect(InetSocketAddress(h.name, h.port), 5000)
                s.soTimeout = 60_000 // eine Minute gar nichts vom Pi (Pi aus, WLAN weg): neu verbinden
                s.startHandshake()
                val input = s.inputStream.bufferedReader(Charsets.UTF_8)
                val chunk = CharArray(4096)
                var buf = ""
                while (gen == generation) {
                    val n = input.read(chunk) // Messzeilen enden mit \r, nicht \n: daher stueckweise lesen
                    if (n < 0) throw IOException(ctx.getString(R.string.pi_closed))
                    buf = onData(buf + String(chunk, 0, n))
                }
            } catch (e: Exception) {
                if (gen != generation) return
                authed = false
                val msg = note.ifEmpty { "$h: ${e.message ?: e.javaClass.simpleName}" }
                state.update { it.copy(status = msg, codeAsked = it.codeAsked && it.page != Page.SETUP) }
                if (state.value.page == Page.SETUP) return // Kopplung: nicht endlos probieren, Fehler zeigen
                Thread.sleep(3000) // Pi weg oder neu gestartet: gleich neu verbinden
            }
        }
    }

    /** Bis zur Anmeldung zeilenweise Antworten, danach Messwerte; liefert den neuen Puffer. */
    private fun onData(text: String): String {
        var buf = text
        while (!authed && '\n' in buf) {
            onReply(buf.substringBefore('\n').trim())
            buf = buf.substringAfter('\n')
        }
        if (!authed) return buf
        val (r, rest) = parse(buf.takeLast(500))
        if (r != null) onReading(r)
        return rest
    }

    private fun onReading(r: Reading) {
        val n = synchronized(history) {
            history += Sample(System.currentTimeMillis(), r)
            history.size
        }
        state.update { it.copy(reading = r, readingAt = SystemClock.elapsedRealtime(), historySize = n) }
    }

    private fun onReply(line: String) {
        val word = line.substringBefore(' ')
        val arg = line.substringAfter(' ', "")
        when (word) {
            "BBQ/2" -> write(if (mode == "pair") "pair Handy ${Build.MODEL}" else "auth $token")
            "code" -> state.update {
                it.copy(codeAsked = true, piId = "${seenFp.take(4)} ${seenFp.drop(4).take(4)}",
                    status = ctx.getString(R.string.code_shown))
            }
            "wrong" -> {
                val n = arg.toIntOrNull() ?: 0
                state.update { it.copy(status = ctx.resources.getQuantityString(R.plurals.wrong_code, n, n), wrongCodes = it.wrongCodes + 1) }
            }
            "expired" -> state.update { it.copy(codeAsked = false, status = ctx.getString(R.string.code_expired)) }
            "busy" -> state.update { it.copy(status = ctx.getString(R.string.busy)) }
            "paired" -> {
                token = arg
                pin = seenFp
                mode = "auth"
                prefs.edit().putString("host", host.toString()).putString("token", token).putString("pin", pin)
                    .putBoolean("direct", false).apply()
            }
            "ok" -> {
                authed = true
                state.update { it.copy(status = "", codeAsked = false, page = it.view, running = true) }
            }
            "denied" -> {
                toSetup(ctx.getString(R.string.not_paired))
                close()
            }
        }
    }

    private fun toSetup(msg: String) {
        note = msg
        state.update { it.copy(page = Page.SETUP, status = msg, codeAsked = false) }
    }

    private fun write(line: String) = out.execute {
        try {
            socket?.outputStream?.apply {
                write("$line\n".toByteArray())
                flush()
            }
        } catch (_: IOException) { // Verbindung weg: run() merkt es und verbindet neu
        }
    }

    /** Der Pi hat ein selbstsigniertes Zertifikat: Statt einer Zertifizierungsstelle zaehlt der beim Koppeln
     *  gemerkte Fingerabdruck. Beim Koppeln selbst schuetzt der Vergleich der Pi-Kennung (Display <-> App). */
    private val trust = object : X509TrustManager {
        override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) = throw CertificateException()

        override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
            seenFp = MessageDigest.getInstance("SHA-256").digest(chain[0].encoded).joinToString("") { "%02X".format(it) }
            if (mode != "pair" && seenFp != pin) {
                toSetup(ctx.getString(R.string.other_cert))
                throw CertificateException("Zertifikat passt nicht zum gekoppelten Pi")
            }
        }

        override fun getAcceptedIssuers() = arrayOf<X509Certificate>()
    }
}
