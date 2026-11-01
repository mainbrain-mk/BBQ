"""Fenster fuer den Rechner: zeigt die Temperaturen vom Pi, Klick auf eine Karte setzt das Ziel.

Qt (PyQt6) uebernimmt Farben und Stil vom System, hell wie dunkel, auch beim Umschalten.
Spricht mit bbq.py per TLS (Protokoll siehe README):

    python3 bbq_client.py [host[:port]]

Adresse: Argument, sonst BBQ_HOST, sonst die gemerkte aus ~/.config/bbq/bbq.env. Ohne Kopplung
zeigt das Fenster links das Logo, rechts "Koppeln": Der Pi zeigt dann einen 6-stelligen Code auf
seinem Display, der hier eingegeben wird. Danach stehen Adresse, Token und Zertifikat-Fingerabdruck
des Pi in ~/.config/bbq/bbq.env (nur fuer den Besitzer lesbar); verbunden wird per TLS nur mit genau
diesem Pi.
"""
import math, os, re, signal, socket, sys, time
from pathlib import Path
from PyQt6.QtCore import QCryptographicHash, QEvent, QPointF, QRectF, QRegularExpression, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPalette, QPen, QPolygonF,
                         QRegularExpressionValidator)
from PyQt6.QtNetwork import QAbstractSocket, QSslSocket
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QProgressBar, QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

PORT = int(os.environ.get("BBQ_PORT", "51235"))
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "bbq" / "bbq.env"
STALE_S = 10  # so lange ohne Messung -> Werte ausgrauen
SILENT_S = 60  # so lange gar nichts vom Pi -> Verbindung neu aufbauen (Pi aus, WLAN weg)
LINE = re.compile(r"P1\s+(\d+) °C \(Ziel (\d+)\)\s+P2\s+(\d+) °C \(Ziel (\d+)\)")
ICON = Path(__file__).resolve().parent / "assets" / "bbq-logo.png"
# Schatten im Kartenhintergrund: Silhouette, Lage (Anteil der Kartenbreite), Neigung in Grad
ART = {1: ("tongs.png", 0.42, -18), 2: ("fork.png", 0.58, 18)}
ART_OPACITY = 0.08
ART_SCALE = 1.2  # Hoehe relativ zur Karte: bewusst groesser, oben und unten gleich angeschnitten
REACHED = QColor("#27ae60")  # Breeze "positiv", auf hell und dunkel lesbar
P2_COLOR = QColor("#f67400")  # Verlauf: P1 in der Akzentfarbe des Systems, P2 Breeze-Orange (passt zum Logo)
GAP_S = 10  # so lange keine Messung -> Luecke im Verlauf (Thermometer aus, Pi weg)
NO_PROBE = 0x7FFF  # Wert eines abgezogenen Fuehlers


def parse(text: str):
    """Letzte vollstaendige Temperaturzeile im Text -> ((p1, p2, t1, t2) oder None, Rest dahinter).

    Den Rest als neuen Puffer behalten: Sonst gilt eine alte Zeile bei jeder spaeteren Meldung
    vom Pi ("nicht gefunden …") wieder als frische Messung.
    """
    m = None
    for m in LINE.finditer(text):
        pass
    if not m:
        return None, text
    p1, t1, p2, t2 = map(int, m.groups())
    return (p1, p2, t1, t2), text[m.end():]


def series(history, i, gap=GAP_S):
    """Verlauf [(zeit, p1, p2, t1, t2)] -> zusammenhaengende Stuecke [(zeit, wert)] fuer Spalte i (1..4).
    Neues Stueck nach einer Messpause oder wenn der Fuehler abgezogen war."""
    parts, cur, last_t = [], [], None
    for s in history:
        t, v = s[0], s[i]
        if v >= NO_PROBE or (last_t is not None and t - last_t > gap):
            if cur:
                parts.append(cur)
            cur = []
        if v < NO_PROBE:
            cur.append((t, v))
        last_t = t
    return parts + [cur] if cur else parts


def ticks(lo, hi, n=5):
    """Runde Werte fuer die Temperaturachse: Schritt 5/10/20/25/50/100, hoechstens etwa n Abschnitte."""
    step = next((s for s in (5, 10, 20, 25, 50, 100) if (hi - lo) / s <= n), 100)
    a, b = math.floor(lo / step) * step, math.ceil(hi / step) * step
    return list(range(a, max(b, a + step) + 1, step))


def read_env(path):
    """KEY=VALUE-Zeilen (# = Kommentar) -> dict; fehlt die Datei: leer."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return {}
    pairs = (line.partition("=") for line in lines)
    return {k.strip(): v.strip() for k, sep, v in pairs if sep and not k.strip().startswith("#")}


def save_env(path, key, value):
    """Setzt KEY=VALUE: ersetzt eine vorhandene Zeile, sonst angehaengt; legt den Ordner an."""
    try:
        lines = [l for l in path.read_text().splitlines() if l.partition("=")[0].strip() != key]
    except FileNotFoundError:
        lines = []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines + [f"{key}={value}"]) + "\n")


def parse_host(text):
    """'car.local', '192.168.0.5:51235', 'tcp://car.local/' -> (host, port); Unbrauchbares -> None."""
    t = text.strip().split("://", 1)[-1].strip("/")
    host, sep, port = t.rpartition(":")  # ponytail: IPv6 ([::1]:port) nicht abgedeckt, bei Bedarf hier
    if sep and port.isdigit() and host:
        return host, int(port)
    return (t, PORT) if t and not any(c.isspace() or c in ":/" for c in t) else None


def start_host():
    """Argument > BBQ_HOST > gemerkte Adresse; nichts davon -> None (Einrichtung im Fenster)."""
    for text in (sys.argv[1] if len(sys.argv) > 1 else "", os.environ.get("BBQ_HOST", ""),
                 read_env(CONFIG).get("BBQ_HOST", "")):
        if text and (h := parse_host(text)):
            return h
    return None


class TargetBox(QSpinBox):
    """Enter oder Fokus woanders (Klick daneben, anderes Fenster) uebernimmt; Esc bricht ab."""
    accepted, cancelled = pyqtSignal(), pyqtSignal()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.interpretText()
            self.accepted.emit()
        elif e.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
        else:
            super().keyPressEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        if e.reason() != Qt.FocusReason.PopupFocusReason:  # Kontextmenue des Feldes laesst es offen
            # erst nach dem Klick schliessen, sonst oeffnet ein Klick in dieselbe Karte das Feld gleich wieder
            QTimer.singleShot(0, self.accepted.emit)


class CornerButton(QAbstractButton):
    """Kleiner Eck-Knopf im Stil der Schatten: Rahmen und Piktogramm in der Textfarbe, mit der Maus kraeftiger.
    icon: "chart" (Achse mit Kurve: zum Verlauf) oder "cards" (zwei Kacheln: zurueck zu den Werten)."""
    SIZE, INSET = 32, 14

    def __init__(self, icon, tip, parent):
        super().__init__(parent)
        self.icon = icon
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setToolTip(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)  # neu zeichnen, wenn die Maus kommt oder geht
        parent.corner = self

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hot = self.underMouse()
        c = self.palette().color(QPalette.ColorRole.Text)
        frame, ink, fill = QColor(c), QColor(c), QColor(c)
        frame.setAlphaF(0.45 if hot else 0.22)
        ink.setAlphaF(0.75 if hot else 0.35)
        fill.setAlphaF(0.06 if hot else 0)
        p.setPen(QPen(frame, 1.2))
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)
        p.setPen(QPen(ink, 2, cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        s = self.SIZE
        if self.icon == "chart":
            p.drawPolyline(QPolygonF([QPointF(8, 8), QPointF(8, s - 8), QPointF(s - 8, s - 8)]))
            p.drawPolyline(QPolygonF([QPointF(11, s - 12), QPointF(15, 17), QPointF(19, 19), QPointF(s - 8, 10)]))
        else:
            p.drawRoundedRect(QRectF(7, 9, 8, 14), 2, 2)
            p.drawRoundedRect(QRectF(17, 9, 8, 14), 2, 2)


class Panel(QFrame):
    """Kachel mit runden Ecken in Systemfarben; Unterklassen malen in paint_inside() darauf."""
    corner = None  # optionaler CornerButton, sitzt immer unten rechts

    def resizeEvent(self, e):
        if self.corner:
            k = CornerButton
            self.corner.move(self.width() - k.INSET - k.SIZE, self.height() - k.INSET - k.SIZE)
            self.corner.raise_()
        super().resizeEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pal = self.palette()
        card = QPainterPath()
        card.addRoundedRect(QRectF(self.rect().adjusted(1, 1, -1, -1)), 14, 14)
        p.setPen(pal.color(QPalette.ColorRole.Mid))
        p.setBrush(pal.color(QPalette.ColorRole.Base))
        p.drawPath(card)
        p.setClipPath(card)
        self.paint_inside(p)

    def paint_inside(self, p):
        pass


class LogoPanel(Panel):
    """Linke Kachel bei der Einrichtung: das farbige Logo, mittig."""

    def __init__(self):
        super().__init__()
        self.logo = QImage(str(ICON))

    def paint_inside(self, p):
        s = min(self.width(), self.height()) * 0.7
        p.drawImage(QRectF((self.width() - s) / 2, (self.height() - s) / 2, s, s), self.logo)


class HistoryPanel(Panel):
    """Temperaturverlauf seit dem Start der App: P1/P2 als Linien, ihre Ziele gestrichelt in derselben Farbe."""

    def __init__(self, history):
        super().__init__()
        self.history = history  # dieselbe Liste, die das Fenster fuellt

    def paint_inside(self, p):
        pal = self.palette()
        dim = pal.color(QPalette.ColorRole.PlaceholderText)
        colors = {1: pal.color(QPalette.ColorRole.Highlight), 2: P2_COLOR}
        f = p.font()
        f.setPixelSize(13)
        p.setFont(f)
        lines = {i: series(self.history, i) for i in (1, 2, 3, 4)}
        vals = [v for parts in lines.values() for part in parts for _, v in part]
        if not vals:
            p.setPen(dim)
            p.drawText(QRectF(self.rect()), Qt.AlignmentFlag.AlignCenter, "Noch keine Messwerte seit dem Start der App")
            return
        # Platz fuer Legende oben, Achsen links/unten und rechts fuer den Eck-Knopf
        area = QRectF(self.rect()).adjusted(52, 44, -(30 + CornerButton.SIZE), -34)
        t0 = self.history[0][0]
        span = max(self.history[-1][0] - t0, 60)
        ys = ticks(min(vals), max(vals))
        lo, hi = ys[0], ys[-1]

        def X(t):
            return area.left() + (t - t0) / span * area.width()

        def Y(v):
            return area.bottom() - (v - lo) / (hi - lo) * area.height()

        grid = QColor(dim)
        grid.setAlpha(60)
        right, vcenter = Qt.AlignmentFlag.AlignRight, Qt.AlignmentFlag.AlignVCenter
        for v in ys:
            p.setPen(grid)
            p.drawLine(QPointF(area.left(), Y(v)), QPointF(area.right(), Y(v)))
            p.setPen(dim)
            p.drawText(QRectF(0, Y(v) - 9, area.left() - 8, 18), right | vcenter, f"{v}°")
        for frac, align in ((0, Qt.AlignmentFlag.AlignLeft), (0.5, Qt.AlignmentFlag.AlignHCenter), (1, right)):
            x = area.left() + frac * area.width()
            box = QRectF(x - 80 * frac, area.bottom() + 6, 80, 18)
            p.drawText(box, align, time.strftime("%H:%M", time.localtime(t0 + frac * span)))
        for i in (3, 4, 1, 2):  # Ziele zuerst, die Messwerte liegen obenauf
            pen = QPen(colors[1 if i in (1, 3) else 2], 2.5 if i < 3 else 1.5)
            if i > 2:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            for part in lines[i]:
                step = max(1, len(part) // max(1, int(area.width())))  # nicht mehr Punkte als Pixel
                pts = part[::step] + part[-1:]
                p.drawPolyline(QPolygonF([QPointF(X(t), Y(v)) for t, v in pts]))
        x = area.left()
        for i, name in ((1, "P1"), (2, "P2")):
            p.setPen(QPen(colors[i], 3))
            p.drawLine(QPointF(x, 22), QPointF(x + 18, 22))
            p.setPen(pal.color(QPalette.ColorRole.Text))
            p.drawText(QPointF(x + 24, 27), name)
            x += 70
        p.setPen(dim)
        p.drawText(QPointF(x, 27), "gestrichelt: Ziel")


class Card(Panel):
    target_set = pyqtSignal(int, int)  # Fuehler, °C

    def __init__(self, probe):
        super().__init__()
        self.probe, self.t = probe, None
        self.art, self.tinted = QImage(str(ICON.parent / ART[probe][0])), (None, None)  # (Farbe, eingefaerbt)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # Klick holt den Fokus -> offenes Feld schliesst
        self.title = QLabel(f"TEMP P{probe}")
        self.title.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.temp = QLabel("--", alignment=Qt.AlignmentFlag.AlignCenter)
        self.bar = QProgressBar(textVisible=False, maximumHeight=6)
        if probe == 2:  # Balken in der Farbe der P2-Linie im Verlauf; P1 hat schon die Akzentfarbe wie seine Linie
            pal = QPalette()  # nur diese Rollen setzen, der Rest folgt weiter dem System
            pal.setColor(QPalette.ColorRole.Highlight, P2_COLOR)
            pal.setColor(QPalette.ColorRole.Accent, P2_COLOR)  # Breeze (Qt 6) malt den Balken in Accent
            self.bar.setPalette(pal)
        self.target = QLabel("Ziel --", alignment=Qt.AlignmentFlag.AlignCenter)
        self.target.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.edit = TargetBox(minimum=1, maximum=249, suffix=" °C", alignment=Qt.AlignmentFlag.AlignCenter)
        self.edit.accepted.connect(self.commit)
        self.edit.cancelled.connect(self.close_edit)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.target)
        self.stack.addWidget(self.edit)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 20)
        lay.addWidget(self.title)
        lay.addWidget(self.temp, 1)
        lay.addWidget(self.bar)
        lay.addWidget(self.stack)

    def paint_inside(self, p):
        if self.art.isNull():  # Bild fehlt: Karte ohne Schatten
            return
        color = self.palette().color(QPalette.ColorRole.Text)
        if self.tinted[0] != color.rgba():  # Silhouette in Textfarbe einfaerben, neu nur beim Farbwechsel
            img = QImage(self.art.size(), QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.transparent)
            q = QPainter(img)
            q.drawImage(0, 0, self.art)
            q.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            q.fillRect(img.rect(), color)
            q.end()
            self.tinted = (color.rgba(), img)
        img, (_, x, angle) = self.tinted[1], ART[self.probe]
        p.setOpacity(ART_OPACITY)
        p.translate(self.width() * x, self.height() * 0.5)  # Bildmitte = Kartenmitte -> gleich viel weg oben wie unten
        p.rotate(angle)
        s = self.height() * ART_SCALE / img.height()
        p.scale(s, s)
        p.drawImage(QPointF(-img.width() / 2, -img.height() / 2), img)

    def resizeEvent(self, e):
        f = self.temp.font()
        f.setPixelSize(max(24, int(self.height() * 0.28)))
        f.setWeight(QFont.Weight.Bold)
        self.temp.setFont(f)
        f = self.target.font()
        f.setPixelSize(max(12, int(self.height() * 0.07)))
        self.target.setFont(f)
        self.edit.setFont(f)
        f.setWeight(QFont.Weight.DemiBold)
        self.title.setFont(f)
        super().resizeEvent(e)

    def show_reading(self, p, t):
        """p, t in °C, oder None, wenn keine frischen Werte da sind."""
        self.t = t
        self.temp.setText("--" if p is None else f"{p} °C")
        self.target.setText("Ziel --" if t is None else f"Ziel {t} °C")
        reached = p is not None and t and p >= t
        pal = QPalette()  # leere Palette = Systemfarben erben
        if reached:
            pal.setColor(QPalette.ColorRole.WindowText, REACHED)
        self.temp.setPalette(pal)
        self.bar.setRange(0, t or 1)
        self.bar.setValue(min(p or 0, t or 0))

    def mousePressEvent(self, _):
        if self.t is not None and self.stack.currentIndex() == 0:
            self.edit.setValue(self.t)
            self.stack.setCurrentIndex(1)
            self.edit.setFocus()
            self.edit.selectAll()

    def close_edit(self):
        self.stack.setCurrentIndex(0)

    def commit(self):
        if self.stack.currentIndex() != 1:  # schon zu (Esc, oder Enter und danach Fokusverlust)
            return
        self.close_edit()
        if self.edit.value() != self.t:
            self.target_set.emit(self.probe, self.edit.value())


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BBQ")
        self.resize(640, 380)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # Klick neben die Karten schliesst ein offenes Feld

        # Seite 0: Kopplung (Logo links, Adresse und Code rechts), Seite 1: die beiden Temperaturkarten
        form = Panel()
        title = QLabel("Pi koppeln")
        f = title.font()
        f.setPixelSize(22)
        f.setWeight(QFont.Weight.Bold)
        title.setFont(f)
        hint = QLabel("Adresse des Raspberry Pi,\nder mit dem Thermometer verbunden ist:", wordWrap=True)
        hint.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.addr = QLineEdit(placeholderText="z. B. raspberrypi.local oder 192.168.0.10")
        self.addr.returnPressed.connect(self.pair)
        go = QPushButton("Koppeln")
        go.clicked.connect(self.pair)
        self.code_row = QWidget()  # erscheint, sobald der Pi seinen Code anzeigt
        cl = QVBoxLayout(self.code_row)
        cl.setContentsMargins(0, 14, 0, 0)
        self.code_hint = QLabel(wordWrap=True)
        self.code = QLineEdit(placeholderText="6-stelliger Code vom Pi-Display", maxLength=6)
        self.code.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,6}")))
        self.code.returnPressed.connect(self.send_code)
        ok = QPushButton("Bestätigen")
        ok.clicked.connect(self.send_code)
        for w in (self.code_hint, self.code, ok):
            cl.addWidget(w)
        self.code_row.hide()
        self.fields = (self.addr, self.code)
        self.recolor_fields()
        fl = QVBoxLayout(form)
        fl.setContentsMargins(24, 20, 24, 24)
        fl.addWidget(title)
        fl.addStretch(1)
        for w in (hint, self.addr, go, self.code_row):
            fl.addWidget(w)
        fl.addStretch(2)
        setup = QWidget()
        srow = QHBoxLayout(setup)
        srow.setContentsMargins(0, 0, 0, 0)
        srow.setSpacing(12)
        srow.addWidget(LogoPanel(), 1)  # gleich breit wie die Karten spaeter; ohne Stretch bliebe sie 0 breit
        srow.addWidget(form, 1)

        self.cards = [Card(1), Card(2)]
        cards = QWidget()
        row = QHBoxLayout(cards)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        for c in self.cards:
            c.target_set.connect(self.send)
            row.addWidget(c)

        self.history = []  # (time.time(), p1, p2, t1, t2) je Messung seit dem Start der App
        self.chart = HistoryPanel(self.history)
        # Eck-Knoepfe: in der rechten Karte zum Verlauf, im Verlauf an derselben Stelle zurueck
        CornerButton("chart", "Temperaturverlauf seit dem Start", self.cards[1]).clicked.connect(self.toggle_view)
        CornerButton("cards", "Zurück zu den Werten", self.chart).clicked.connect(self.toggle_view)

        self.pages = QStackedWidget()  # 0 Kopplung, 1 Karten, 2 Verlauf
        self.pages.addWidget(setup)
        self.pages.addWidget(cards)
        self.pages.addWidget(self.chart)
        self.view = 1  # Karten oder Verlauf: dorthin nach der Anmeldung
        self.info = QLabel()
        self.info.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.addWidget(self.pages, 1)
        lay.addWidget(self.info)

        self.buf, self.reading, self.status, self.note, self.last_rx = "", None, "", "", time.monotonic()
        self.retry, self.authed, self.mode = False, False, "auth"  # mode: "auth" (Token) oder "pair" (Code)
        self.sock = QSslSocket(self)
        self.sock.readyRead.connect(self.read)
        self.sock.sslErrors.connect(self.check_cert)
        self.sock.errorOccurred.connect(self.lost)  # auch "Pi hat getrennt"
        timer = QTimer(self, interval=500, timeout=self.tick)
        timer.start()
        self.host = start_host()  # (host, port) oder None
        cfg = read_env(CONFIG)
        paired = bool(self.host and cfg.get("BBQ_TOKEN") and cfg.get("BBQ_HOST") == self.label())
        self.token, self.pin = (cfg["BBQ_TOKEN"], cfg.get("BBQ_PIN", "")) if paired else ("", "")
        if self.host:
            self.addr.setText(self.label())
        if paired:
            self.pages.setCurrentIndex(1)
            self.connect_pi()
        else:
            self.addr.setFocus()

    def recolor_fields(self):
        """Die Felder liegen auf einer Kachel, die schon die Base-Farbe hat: Fensterfarbe nehmen, sonst
        verschwinden sie. Nur Base setzen, alle anderen Farben erben sie weiter vom System."""
        pal = QPalette()
        pal.setColor(QPalette.ColorRole.Base, self.palette().color(QPalette.ColorRole.Window))
        for f in self.fields:
            f.setPalette(pal)

    def changeEvent(self, e):
        if e.type() == QEvent.Type.PaletteChange:  # Farbschema gewechselt
            self.recolor_fields()
        super().changeEvent(e)

    def label(self):
        host, port = self.host
        return host if port == PORT else f"{host}:{port}"

    def pair(self):
        """Knopf "Koppeln": verbinden und um einen Code bitten, den der Pi auf seinem Display zeigt."""
        self.host = parse_host(self.addr.text())
        if not self.host:
            self.status = "Bitte Hostname oder IP-Adresse eingeben, z. B. raspberrypi.local"
            return
        self.mode = "pair"
        self.code_row.hide()
        self.connect_pi()

    def send_code(self):
        if len(self.code.text()) == 6 and self.sock.state() == QAbstractSocket.SocketState.ConnectedState:
            self.sock.write(f"code {self.code.text()}\n".encode())

    def connect_pi(self):
        self.retry, self.authed, self.buf, self.note = False, False, "", ""
        self.status = f"verbinde mit {self.label()} …"
        self.last_rx = time.monotonic()
        self.sock.abort()
        self.sock.connectToHostEncrypted(*self.host)

    def fingerprint(self):
        digest = self.sock.peerCertificate().digest(QCryptographicHash.Algorithm.Sha256)
        return bytes(digest.toHex()).decode().upper()

    def check_cert(self, _errors):
        """Der Pi hat ein selbstsigniertes Zertifikat: Statt einer Zertifizierungsstelle zaehlt der beim
        Koppeln gemerkte Fingerabdruck. Beim Koppeln selbst schuetzt der Vergleich der Pi-Kennung."""
        if self.mode == "pair" or (self.pin and self.fingerprint() == self.pin):
            self.sock.ignoreSslErrors()
        else:  # ohne ignoreSslErrors bricht Qt den Handshake ab, lost() zeigt dann den Hinweis
            self.to_setup("Der Pi meldet sich mit einem anderen Zertifikat als beim Koppeln. "
                          "Neu koppeln, wenn er neu eingerichtet wurde.")

    def to_setup(self, note):
        self.note = self.status = note
        self.code_row.hide()
        self.pages.setCurrentIndex(0)

    def remember(self, **values):
        try:
            for k, v in values.items():
                save_env(CONFIG, k, v)
            CONFIG.chmod(0o600)  # das Token ist ein Geheimnis
        except OSError as e:
            print(f"{CONFIG}: {e}", file=sys.stderr)

    def on_reply(self, line):
        """Antworten des Pi bis zur Anmeldung; danach kommen nur noch Messwerte und Meldungen."""
        word, _, arg = line.partition(" ")
        if word == "BBQ/2":
            hello = f"pair {socket.gethostname()}" if self.mode == "pair" else f"auth {self.token}"
            self.sock.write(f"{hello}\n".encode())
        elif word == "code":
            fp = self.fingerprint()
            self.code_hint.setText(f"Pi-Kennung {fp[:4]} {fp[4:8]}: muss mit der auf dem Pi-Display "
                                   "übereinstimmen.\nCode vom Pi-Display:")
            self.code.clear()
            self.code_row.show()
            self.code.setFocus()
            self.status = "Der Code steht jetzt auf dem Pi-Display (2 Minuten gültig)."
        elif word == "wrong":
            self.code.clear()
            self.status = f"Falscher Code, noch {arg} Versuch(e)."
        elif word == "expired":
            self.code_row.hide()
            self.status = "Code abgelaufen oder zu oft falsch: bitte erneut koppeln."
        elif word == "busy":
            self.status = "Zu viele Fehlversuche: in 30 Sekunden erneut koppeln."
        elif word == "paired":
            self.token, self.pin, self.mode = arg, self.fingerprint(), "auth"
            self.remember(BBQ_HOST=self.label(), BBQ_TOKEN=self.token, BBQ_PIN=self.pin)
        elif word == "ok":
            self.authed, self.status = True, ""
            self.code_row.hide()
            self.pages.setCurrentIndex(self.view)
        elif word == "denied":
            self.to_setup("Dieser Rechner ist auf dem Pi nicht (mehr) gekoppelt: bitte koppeln.")
            self.sock.abort()

    def lost(self, *_):
        self.authed = False
        if self.pages.currentIndex() == 0:  # Kopplung: nicht endlos probieren, Fehler zeigen
            self.status = self.note or f"{self.label()}: {self.sock.errorString()}"
            self.code_row.hide()
        elif not self.retry:  # Pi weg oder neu gestartet: in 3 s neu verbinden
            self.retry = True
            self.status = f"{self.label()}: {self.sock.errorString()}"
            QTimer.singleShot(3000, self.connect_pi)

    def read(self):
        self.last_rx = time.monotonic()
        self.buf += bytes(self.sock.readAll()).decode(errors="replace")
        while not self.authed and "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.on_reply(line.strip())
        if self.authed:
            r, self.buf = parse(self.buf[-500:])
            if r:
                self.reading = (r, time.monotonic())
                self.history.append((time.time(), *r))

    def toggle_view(self):
        """Eck-Knopf: zwischen den Karten und dem Temperaturverlauf wechseln."""
        self.view = 2 if self.view == 1 else 1
        if self.pages.currentIndex() != 0:
            self.pages.setCurrentIndex(self.view)

    def send(self, probe, t):
        if self.authed:
            self.sock.write(f"p{probe} {t}\n".encode())

    def tick(self):
        now = time.monotonic()
        if self.authed and now - self.last_rx > SILENT_S:  # beim Code-Eintippen schweigt der Pi zu Recht
            self.lost()
        fresh = self.reading is not None and now - self.reading[1] < STALE_S
        for i, c in enumerate(self.cards):
            r = self.reading[0] if fresh else None
            c.show_reading(r and r[i], r and r[2 + i])
        page = self.pages.currentIndex()
        waiting = not fresh and page != 0
        self.info.setText(self.status or ("warte auf Messwerte …" if waiting else ""))
        self.info.setVisible(bool(self.info.text()))  # leer: kein Platz unten verschenken
        if page == 2:
            self.chart.update()


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Strg+C im Terminal beendet sofort (Qt-Schleife faengt es sonst nicht)
    app = QApplication(sys.argv)
    app.setApplicationName("BBQ")
    app.setDesktopFileName("bbq-client")  # Wayland: Fenster-Icon kommt aus bbq-client.desktop
    app.setWindowIcon(QIcon(str(ICON)))
    w = Window()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    if os.environ.get("BBQ_SELFTEST"):
        line = "\r16:21:08   P1  23 °C (Ziel 75)   P2  19 °C (Ziel 80)   "
        half = line.replace(" 23 ", "  24 ")[:20]
        r, rest = parse("\nverbunden\n" + line + half)
        assert r == (23, 19, 75, 80) and rest.endswith(half)  # halbe Zeile bleibt fuer spaeter
        assert parse("\nverbunden\n") == (None, "\nverbunden\n")
        _, rest = parse(line)
        assert parse(rest + "\nnicht gefunden …\n")[0] is None  # alte Zeile zaehlt nicht noch einmal
        h = [(0, 20, 30, 65, 65), (1, 21, NO_PROBE, 65, 65), (2, 22, 31, 65, 65), (30, 40, 32, 70, 65)]
        assert series(h, 1) == [[(0, 20), (1, 21), (2, 22)], [(30, 40)]]  # Pause > 10 s: Luecke
        assert series(h, 2) == [[(0, 30)], [(2, 31)], [(30, 32)]]  # Fuehler abgezogen: Luecke
        assert series([], 1) == []
        assert ticks(19, 23) == [15, 20, 25] and ticks(20, 188) == [0, 50, 100, 150, 200] and ticks(65, 65) == [65, 70]
        assert parse_host(" car.local ") == ("car.local", PORT)
        assert parse_host("192.168.0.5:4000") == ("192.168.0.5", 4000)
        assert parse_host("tcp://car.local:51235/") == ("car.local", 51235)
        assert parse_host("") is None and parse_host("zwei worte") is None and parse_host("a:b:c") is None
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / "neu" / "bbq.env"
            assert read_env(env) == {}
            save_env(env, "BBQ_HOST", "car.local")
            env.write_text(env.read_text() + "# Kommentar\nBBQ_PORT=4000\n")
            save_env(env, "BBQ_HOST", "192.168.0.5")  # ersetzt, Rest bleibt
            assert read_env(env) == {"BBQ_HOST": "192.168.0.5", "BBQ_PORT": "4000"}
        print("ok")
    else:
        main()
