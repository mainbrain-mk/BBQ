package io.github.mainbrain_mk.bbq

import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.max

// Gleiches Protokoll wie bbq_client.py (siehe README): reine Funktionen, ohne Android, mit Unit-Test.

const val DEFAULT_PORT = 51235
const val NO_PROBE = 0x7FFF // Wert eines abgezogenen Fuehlers
const val STALE_MS = 10_000L // so lange ohne Messung -> Werte ausgrauen
const val GAP_MS = 10_000L // so lange keine Messung -> Luecke im Verlauf

data class Reading(val p1: Int, val p2: Int, val t1: Int, val t2: Int) {
    fun probe(i: Int) = if (i == 1) p1 else p2
    fun target(i: Int) = if (i == 1) t1 else t2
}

data class Sample(val time: Long, val r: Reading)

data class Host(val name: String, val port: Int = DEFAULT_PORT) {
    override fun toString() = if (port == DEFAULT_PORT) name else "$name:$port"
}

private val LINE = Regex("""P1\s+(\d+) °C \(Ziel (\d+)\)\s+P2\s+(\d+) °C \(Ziel (\d+)\)""")

// Direkt per Bluetooth wie in bbq.py: Namen des Thermometers (Werbung / nach dem Verbinden) und seine Pakete
val NAMES = setOf("Day_BBQ", "Day_BQQ")

/** Notify-Paket: <4H = Ist P1, Ist P2, Ziel P1, Ziel P2 in °C, dahinter 2 unbekannte Bytes; zu kurz -> null. */
fun decode(b: ByteArray): Reading? {
    if (b.size < 8) return null
    fun u16(i: Int) = (b[i].toInt() and 0xFF) or ((b[i + 1].toInt() and 0xFF) shl 8)
    return Reading(u16(0), u16(2), u16(4), u16(6))
}

/** Ziel setzen: [Ziel P1, 0, Ziel P2, 0, 0, Summe]; 0xFA heisst "Fuehler nicht aendern", daher max. 249 °C. */
fun targetCommand(probe: Int, t: Int): ByteArray {
    val b = if (probe == 1) intArrayOf(t, 0, 0xFA, 0, 0) else intArrayOf(0xFA, 0, t, 0, 0)
    return (b + (b.sum() and 0xFF)).map { it.toByte() }.toByteArray()
}

/** Letzte vollstaendige Temperaturzeile im Text -> (Messung oder null, Rest dahinter). Den Rest als
 *  neuen Puffer behalten: sonst gilt eine alte Zeile bei jeder spaeteren Meldung wieder als frisch. */
fun parse(text: String): Pair<Reading?, String> {
    val m = LINE.findAll(text).lastOrNull() ?: return null to text
    val (p1, t1, p2, t2) = m.destructured
    return Reading(p1.toInt(), p2.toInt(), t1.toInt(), t2.toInt()) to text.substring(m.range.last + 1)
}

/** "car.local", "192.168.0.5:51235", "tcp://car.local/" -> Host; Unbrauchbares -> null (kein IPv6). */
fun parseHost(text: String): Host? {
    val t = text.trim().substringAfter("://").trim('/')
    val i = t.lastIndexOf(':')
    val port = if (i > 0) t.substring(i + 1) else ""
    if (port.isNotEmpty() && port.all(Char::isDigit)) return Host(t.substring(0, i), port.toInt())
    return if (t.isNotEmpty() && t.none { it.isWhitespace() || it == ':' || it == '/' }) Host(t) else null
}

/** Verlauf -> zusammenhaengende Stuecke (zeit, wert) fuer Spalte i (1, 2 = Ist P1/P2; 3, 4 = Ziel P1/P2).
 *  Neues Stueck nach einer Messpause oder wenn der Fuehler abgezogen war. */
fun series(history: List<Sample>, i: Int, gap: Long = GAP_MS): List<List<Pair<Long, Int>>> {
    val parts = mutableListOf<List<Pair<Long, Int>>>()
    var cur = mutableListOf<Pair<Long, Int>>()
    var last: Long? = null
    for (s in history) {
        val v = when (i) { 1 -> s.r.p1; 2 -> s.r.p2; 3 -> s.r.t1; else -> s.r.t2 }
        if (v >= NO_PROBE || (last != null && s.time - last > gap)) {
            if (cur.isNotEmpty()) parts += cur
            cur = mutableListOf()
        }
        if (v < NO_PROBE) cur += s.time to v
        last = s.time
    }
    if (cur.isNotEmpty()) parts += cur
    return parts
}

/** Runde Werte fuer die Temperaturachse: Schritt 5/10/20/25/50/100, hoechstens etwa n Abschnitte. */
fun ticks(lo: Int, hi: Int, n: Int = 5): List<Int> {
    val step = listOf(5, 10, 20, 25, 50, 100).firstOrNull { (hi - lo).toDouble() / it <= n } ?: 100
    val a = floor(lo.toDouble() / step).toInt() * step
    val b = max(ceil(hi.toDouble() / step).toInt() * step, a + step)
    return (a..b step step).toList()
}
