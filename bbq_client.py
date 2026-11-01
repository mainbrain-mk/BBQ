"""Fenster fuer den Rechner: zeigt die Grilltemperaturen vom Pi, Klick auf eine Karte setzt das Ziel.

Qt (PyQt6) uebernimmt Farben und Stil vom System, hell wie dunkel, auch beim Umschalten.
Spricht dasselbe TCP-Protokoll wie `nc <pi>.local 51235`:

    python3 bbq_client.py [host]    # oder BBQ_HOST setzen; Standard raspberrypi.local
"""
import os, re, signal, sys, time
from pathlib import Path
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPalette
from PyQt6.QtNetwork import QAbstractSocket, QTcpSocket
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QProgressBar, QSpinBox,
                             QStackedWidget, QVBoxLayout, QWidget)

HOST = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BBQ_HOST", "raspberrypi.local")
PORT = int(os.environ.get("BBQ_PORT", "51235"))
STALE_S = 10  # so lange ohne Messung -> Werte ausgrauen
SILENT_S = 60  # so lange gar nichts vom Pi -> Verbindung neu aufbauen (Pi aus, WLAN weg)
LINE = re.compile(r"P1\s+(\d+) °C \(Ziel (\d+)\)\s+P2\s+(\d+) °C \(Ziel (\d+)\)")
ICON = Path(__file__).resolve().parent / "assets" / "bbq-logo.png"
REACHED = QColor("#27ae60")  # Breeze "positiv", auf hell und dunkel lesbar


def parse(text: str):
    """Letzte vollstaendige Temperaturzeile im Text -> (p1, p2, t1, t2) oder None."""
    found = LINE.findall(text)
    if not found:
        return None
    p1, t1, p2, t2 = map(int, found[-1])
    return p1, p2, t1, t2


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


class Card(QFrame):
    target_set = pyqtSignal(int, int)  # Fuehler, °C

    def __init__(self, probe):
        super().__init__()
        self.probe, self.t = probe, None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # Klick holt den Fokus -> offenes Feld schliesst
        self.title = QLabel(f"TEMP P{probe}")
        self.title.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.temp = QLabel("--", alignment=Qt.AlignmentFlag.AlignCenter)
        self.bar = QProgressBar(textVisible=False, maximumHeight=6)
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

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        p.setPen(pal.color(QPalette.ColorRole.Mid))
        p.setBrush(pal.color(QPalette.ColorRole.Base))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 14, 14)

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
        self.setWindowTitle("Grill")
        self.resize(640, 380)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # Klick neben die Karten schliesst ein offenes Feld
        self.cards = [Card(1), Card(2)]
        row = QHBoxLayout()
        row.setSpacing(12)
        for c in self.cards:
            c.target_set.connect(self.send)
            row.addWidget(c)
        self.info = QLabel()
        self.info.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.addLayout(row, 1)
        lay.addWidget(self.info)

        self.buf, self.reading, self.status, self.last_rx = "", None, "", time.monotonic()
        self.retry = False
        self.sock = QTcpSocket(self)
        self.sock.readyRead.connect(self.read)
        self.sock.connected.connect(lambda: setattr(self, "status", ""))
        self.sock.errorOccurred.connect(self.lost)  # auch "Pi hat getrennt"
        timer = QTimer(self, interval=500, timeout=self.tick)
        timer.start()
        self.connect_pi()

    def connect_pi(self):
        self.retry = False
        self.status = f"verbinde mit {HOST} …"
        self.last_rx = time.monotonic()
        self.sock.abort()
        self.sock.connectToHost(HOST, PORT)

    def lost(self, *_):
        if not self.retry:  # Pi weg oder neu gestartet: in 3 s neu verbinden
            self.retry = True
            self.status = f"{HOST}: {self.sock.errorString()}"
            QTimer.singleShot(3000, self.connect_pi)

    def read(self):
        self.last_rx = time.monotonic()
        self.buf = (self.buf + bytes(self.sock.readAll()).decode(errors="replace"))[-500:]
        if r := parse(self.buf):
            self.reading = (r, time.monotonic())

    def send(self, probe, t):
        if self.sock.state() == QAbstractSocket.SocketState.ConnectedState:
            self.sock.write(f"p{probe} {t}\n".encode())

    def tick(self):
        now = time.monotonic()
        connected = self.sock.state() == QAbstractSocket.SocketState.ConnectedState
        if connected and now - self.last_rx > SILENT_S:
            self.lost()
        fresh = self.reading is not None and now - self.reading[1] < STALE_S
        for i, c in enumerate(self.cards):
            r = self.reading[0] if fresh else None
            c.show_reading(r and r[i], r and r[2 + i])
        self.info.setText(self.status or ("" if fresh else "warte auf Messwerte …"))
        self.info.setVisible(bool(self.info.text()))  # leer: kein Platz unten verschenken


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Strg+C im Terminal beendet sofort (Qt-Schleife faengt es sonst nicht)
    app = QApplication(sys.argv)
    app.setApplicationName("Grill")
    app.setDesktopFileName("bbq-client")  # Wayland: Fenster-Icon kommt aus bbq-client.desktop
    app.setWindowIcon(QIcon(str(ICON)))
    w = Window()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    if os.environ.get("BBQ_SELFTEST"):
        line = "\r16:21:08   P1  23 °C (Ziel 75)   P2  19 °C (Ziel 80)   "
        assert parse("\nverbunden\n" + line + line.replace(" 23 ", "  24 ")[:20]) == (23, 19, 75, 80)  # halbe Zeile ignoriert
        assert parse("\nverbunden\n") is None
        print("ok")
    else:
        main()
