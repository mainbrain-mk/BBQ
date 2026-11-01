package io.github.mainbrain_mk.bbq

import android.Manifest
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.requiredHeight
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ColorFilter
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.delay
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.max

val P2_COLOR = Color(0xFFF67400) // wie in bbq_client.py: P2 orange, P1 in der Akzentfarbe des Systems
val REACHED = Color(0xFF27AE60)

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        Bbq.init(applicationContext)
        setContent { BbqTheme { App(::startService) } }
    }

    override fun onResume() {
        super.onResume()
        if (Bbq.paired && !Bbq.state.value.running) startService() // z. B. nach "Beenden" in der Benachrichtigung
    }

    private fun startService() = ContextCompat.startForegroundService(this, Intent(this, BbqService::class.java))
}

/** Material You: hell/dunkel wie das System, ab Android 12 in den Farben des Hintergrundbilds. */
@Composable
fun BbqTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val ctx = LocalContext.current
    val scheme = when {
        Build.VERSION.SDK_INT >= 31 -> if (dark) dynamicDarkColorScheme(ctx) else dynamicLightColorScheme(ctx)
        dark -> darkColorScheme()
        else -> lightColorScheme()
    }
    MaterialTheme(colorScheme = scheme, content = content)
}

@Composable
fun App(startService: () -> Unit) {
    val s by Bbq.state.collectAsStateWithLifecycle()
    val askNotify = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
    LaunchedEffect(Unit) {
        if (Build.VERSION.SDK_INT >= 33) askNotify.launch(Manifest.permission.POST_NOTIFICATIONS) // fuer den Alarm
    }
    LaunchedEffect(s.page) {
        if (s.page != Page.SETUP && Bbq.paired) startService() // gerade gekoppelt: Dienst haelt die Verbindung
    }
    var now by remember { mutableLongStateOf(SystemClock.elapsedRealtime()) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(500)
            now = SystemClock.elapsedRealtime()
        }
    }
    val fresh = s.reading != null && now - s.readingAt < STALE_MS
    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Column(Modifier.fillMaxSize().safeDrawingPadding().padding(12.dp)) {
            Box(Modifier.weight(1f)) {
                when (s.page) {
                    Page.SETUP -> Setup(s)
                    Page.CARDS -> Cards(if (fresh) s.reading else null)
                    Page.CHART -> Chart(s.historySize)
                }
            }
            val info = s.status.ifEmpty { if (!fresh && s.page != Page.SETUP) "warte auf Messwerte …" else "" }
            if (info.isNotEmpty()) {
                Text(info, color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(top = 8.dp))
            }
        }
    }
}

/** Zwei Kacheln: im Hochformat untereinander, im Querformat nebeneinander. */
@Composable
fun TwoTiles(firstWeight: Float, first: @Composable (Modifier) -> Unit, second: @Composable (Modifier) -> Unit) {
    BoxWithConstraints(Modifier.fillMaxSize()) {
        val gap = Arrangement.spacedBy(12.dp)
        if (maxWidth > maxHeight) {
            Row(Modifier.fillMaxSize(), horizontalArrangement = gap) {
                first(Modifier.weight(1f).fillMaxHeight())
                second(Modifier.weight(1f).fillMaxHeight())
            }
        } else {
            Column(Modifier.fillMaxSize(), verticalArrangement = gap) {
                first(Modifier.weight(firstWeight).fillMaxWidth())
                second(Modifier.weight(1f).fillMaxWidth())
            }
        }
    }
}

@Composable
fun Tile(modifier: Modifier, onClick: (() -> Unit)? = null, content: @Composable BoxScope.() -> Unit) {
    val shape = RoundedCornerShape(14.dp)
    val cs = MaterialTheme.colorScheme
    val m = modifier.clip(shape).background(cs.surfaceContainerLow).border(1.dp, cs.outlineVariant, shape)
    Box(if (onClick != null) m.clickable(onClick = onClick) else m, content = content)
}

@Composable
fun Setup(s: UiState) {
    val cs = MaterialTheme.colorScheme
    var addr by rememberSaveable { mutableStateOf(s.address) }
    var code by rememberSaveable { mutableStateOf("") }
    LaunchedEffect(s.status) { if (s.status.startsWith("Falscher Code")) code = "" }
    TwoTiles(0.6f, { m ->
        Tile(m) {
            Image(painterResource(R.drawable.logo), "BBQ", Modifier.align(Alignment.Center).fillMaxSize(0.7f))
        }
    }, { m ->
        Tile(m) {
            Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("Pi koppeln", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
                Text("Adresse des Raspberry Pi,\nder mit dem Thermometer verbunden ist:", color = cs.onSurfaceVariant)
                OutlinedTextField(addr, { addr = it }, Modifier.fillMaxWidth(), singleLine = true,
                    placeholder = { Text("z. B. raspberrypi.local oder 192.168.0.10") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Go),
                    keyboardActions = KeyboardActions(onGo = { Bbq.pair(addr) }))
                Button({ code = ""; Bbq.pair(addr) }, Modifier.fillMaxWidth()) { Text("Koppeln") }
                if (s.codeAsked) {
                    Text("Pi-Kennung ${s.piId}: muss mit der auf dem Pi-Display übereinstimmen.", fontWeight = FontWeight.SemiBold)
                    OutlinedTextField(code, { if (it.length <= 6 && it.all(Char::isDigit)) code = it }, Modifier.fillMaxWidth(),
                        singleLine = true, label = { Text("Code vom Pi-Display") },
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword, imeAction = ImeAction.Done),
                        keyboardActions = KeyboardActions(onDone = { Bbq.sendCode(code) }))
                    Button({ Bbq.sendCode(code) }, Modifier.fillMaxWidth(), enabled = code.length == 6) { Text("Bestätigen") }
                }
            }
        }
    })
}

@Composable
fun Cards(r: Reading?) = TwoTiles(1f, { m -> ProbeCard(1, r, m) }, { m -> ProbeCard(2, r, m) })

@Composable
fun ProbeCard(probe: Int, r: Reading?, modifier: Modifier) {
    val cs = MaterialTheme.colorScheme
    val p = r?.probe(probe)?.takeIf { it < NO_PROBE }
    val t = r?.target(probe)
    var edit by remember { mutableStateOf(false) }
    Tile(modifier, onClick = { if (t != null) edit = true }) {
        BoxWithConstraints(Modifier.fillMaxSize()) {
            // Schatten: Zange (P1) bzw. Gabel (P2), bewusst gross und oben/unten gleich angeschnitten
            val (img, x, angle) = if (probe == 1) Triple(R.drawable.tongs, 0.42f, -18f) else Triple(R.drawable.fork, 0.58f, 18f)
            Image(painterResource(img), null, Modifier.align(Alignment.Center).offset(x = maxWidth * (x - 0.5f))
                .requiredHeight(maxHeight * 1.2f).rotate(angle),
                contentScale = ContentScale.FillHeight, alpha = 0.08f, colorFilter = ColorFilter.tint(cs.onSurface))
            Column(Modifier.fillMaxSize().padding(horizontal = 20.dp, vertical = 16.dp)) {
                Text("TEMP P$probe", color = cs.onSurfaceVariant, fontWeight = FontWeight.SemiBold,
                    style = MaterialTheme.typography.titleMedium)
                BoxWithConstraints(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                    val size = with(LocalDensity.current) { minOf(maxHeight * 0.6f, maxWidth / 3.4f).toSp() }
                    val reached = p != null && t != null && t > 0 && p >= t
                    Text(if (p == null) "--" else "$p °C", fontSize = size, fontWeight = FontWeight.Bold, maxLines = 1,
                        color = if (reached) REACHED else cs.onSurface)
                }
                LinearProgressIndicator(
                    progress = { if (p == null || t == null || t <= 0) 0f else (p.toFloat() / t).coerceIn(0f, 1f) },
                    Modifier.fillMaxWidth(), color = if (probe == 2) P2_COLOR else cs.primary)
                Text(if (t == null) "Ziel --" else "Ziel $t °C", Modifier.fillMaxWidth().padding(top = 8.dp),
                    color = cs.onSurfaceVariant, style = MaterialTheme.typography.titleLarge, textAlign = TextAlign.Center)
            }
            if (probe == 2) CornerButton(chart = true, Modifier.align(Alignment.BottomEnd).padding(12.dp)) { Bbq.toggleView() }
        }
    }
    if (edit && t != null) TargetDialog(probe, t) { edit = false }
}

@Composable
fun TargetDialog(probe: Int, current: Int, onDismiss: () -> Unit) {
    var text by remember { mutableStateOf(current.toString()) }
    val v = text.toIntOrNull()?.takeIf { it in 1..249 }
    val set = {
        if (v != null) {
            Bbq.setTarget(probe, v)
            onDismiss()
        }
    }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("Ziel P$probe") },
        text = {
            OutlinedTextField(text, { if (it.length <= 3 && it.all(Char::isDigit)) text = it }, singleLine = true,
                suffix = { Text("°C") }, supportingText = { Text("1 … 249 °C") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number, imeAction = ImeAction.Done),
                keyboardActions = KeyboardActions(onDone = { set() }))
        },
        confirmButton = { TextButton(set, enabled = v != null) { Text("Setzen") } },
        dismissButton = { TextButton(onDismiss) { Text("Abbrechen") } })
}

/** Kleiner Eck-Knopf im Stil der Schatten: Achse mit Kurve (zum Verlauf) oder zwei Kacheln (zurueck). */
@Composable
fun CornerButton(chart: Boolean, modifier: Modifier, onClick: () -> Unit) {
    val c = MaterialTheme.colorScheme.onSurface
    val shape = RoundedCornerShape(8.dp)
    Box(modifier.size(36.dp).clip(shape).border(1.dp, c.copy(alpha = 0.22f), shape).clickable(onClick = onClick)) {
        Canvas(Modifier.fillMaxSize()) {
            val u = size.width / 32f // Piktogramm auf einem 32er-Raster wie in bbq_client.py
            val ink = c.copy(alpha = 0.45f)
            val st = Stroke(2.dp.toPx(), cap = StrokeCap.Round, join = StrokeJoin.Round)
            if (chart) {
                drawPath(Path().apply { moveTo(8 * u, 8 * u); lineTo(8 * u, 24 * u); lineTo(24 * u, 24 * u) }, ink, style = st)
                drawPath(Path().apply { moveTo(11 * u, 20 * u); lineTo(15 * u, 17 * u); lineTo(19 * u, 19 * u); lineTo(24 * u, 10 * u) },
                    ink, style = st)
            } else {
                for (x0 in listOf(7f, 17f)) {
                    drawRoundRect(ink, Offset(x0 * u, 9 * u), Size(8 * u, 14 * u), CornerRadius(2 * u), style = st)
                }
            }
        }
    }
}

/** Temperaturverlauf seit dem Start der App: P1/P2 als Linien, ihre Ziele gestrichelt in derselben Farbe. */
@Composable
fun Chart(historySize: Int) {
    val cs = MaterialTheme.colorScheme
    val history = remember(historySize) { Bbq.historySnapshot() }
    val measurer = rememberTextMeasurer()
    val label = TextStyle(color = cs.onSurfaceVariant, fontSize = 12.sp)
    val legend = TextStyle(color = cs.onSurface, fontSize = 13.sp)
    val clock = remember { SimpleDateFormat("HH:mm", Locale.getDefault()) }
    val lines = remember(history) { (1..4).associateWith { series(history, it) } }
    val vals = lines.values.flatten().flatten().map { it.second }
    Tile(Modifier.fillMaxSize()) {
        if (vals.isEmpty()) {
            Text("Noch keine Messwerte seit dem Start der App", Modifier.align(Alignment.Center), color = cs.onSurfaceVariant)
        } else {
            Canvas(Modifier.fillMaxSize()) {
                val left = 52.dp.toPx()
                val top = 44.dp.toPx()
                val right = size.width - (30 + 36).dp.toPx() // rechts Platz fuer den Eck-Knopf
                val bottom = size.height - 34.dp.toPx()
                val t0 = history.first().time
                val span = max(history.last().time - t0, 60_000L).toFloat()
                val ys = ticks(vals.min(), vals.max())
                val lo = ys.first()
                val hi = ys.last()
                fun x(t: Long) = left + (t - t0) / span * (right - left)
                fun y(v: Int) = bottom - (v - lo).toFloat() / (hi - lo) * (bottom - top)
                for (v in ys) {
                    drawLine(cs.onSurfaceVariant.copy(alpha = 0.25f), Offset(left, y(v)), Offset(right, y(v)), 1.dp.toPx())
                    val m = measurer.measure("$v°", label)
                    drawText(m, topLeft = Offset(left - 8.dp.toPx() - m.size.width, y(v) - m.size.height / 2))
                }
                for (frac in listOf(0f, 0.5f, 1f)) {
                    val m = measurer.measure(clock.format(Date(t0 + (frac * span).toLong())), label)
                    drawText(m, topLeft = Offset(left + frac * (right - left) - frac * m.size.width, bottom + 6.dp.toPx()))
                }
                for (i in listOf(3, 4, 1, 2)) { // Ziele zuerst, die Messwerte liegen obenauf
                    val color = if (i == 1 || i == 3) cs.primary else P2_COLOR
                    val st = if (i < 3) Stroke(2.5.dp.toPx(), cap = StrokeCap.Round, join = StrokeJoin.Round)
                    else Stroke(1.5.dp.toPx(), pathEffect = PathEffect.dashPathEffect(floatArrayOf(6.dp.toPx(), 4.dp.toPx())))
                    for (part in lines.getValue(i)) {
                        val step = max(1, part.size / max(1, (right - left).toInt())) // nicht mehr Punkte als Pixel
                        val pts = part.filterIndexed { k, _ -> k % step == 0 } + part.last()
                        drawPath(Path().apply {
                            moveTo(x(pts[0].first), y(pts[0].second))
                            for ((t, v) in pts.drop(1)) lineTo(x(t), y(v))
                        }, color, style = st)
                    }
                }
                var lx = left
                for ((i, name) in listOf(1 to "P1", 2 to "P2")) {
                    drawLine(if (i == 1) cs.primary else P2_COLOR, Offset(lx, 22.dp.toPx()), Offset(lx + 18.dp.toPx(), 22.dp.toPx()),
                        3.dp.toPx())
                    val m = measurer.measure(name, legend)
                    drawText(m, topLeft = Offset(lx + 24.dp.toPx(), 22.dp.toPx() - m.size.height / 2))
                    lx += 70.dp.toPx()
                }
                val m = measurer.measure("gestrichelt: Ziel", label)
                drawText(m, topLeft = Offset(lx, 22.dp.toPx() - m.size.height / 2))
            }
        }
        CornerButton(chart = false, Modifier.align(Alignment.BottomEnd).padding(12.dp)) { Bbq.toggleView() }
    }
}
