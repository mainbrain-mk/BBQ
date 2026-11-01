# Day_BBQ-Fleischthermometer im Netz

<img src="assets/bbq-logo.png" width="96" align="right" alt="Logo: gekreuzte Grillzange und Fleischgabel">

Für das Bluetooth-Fleischthermometer „Day_BBQ“ (Boomex, Artikel 57675, meldet sich per BLE als `Day_BBQ` bzw. `Day_BQQ`)
gibt es keine funktionierende Android-App mehr. Dieses Projekt ersetzt sie:

- **`bbq.py`** läuft auf einem Raspberry Pi (oder jedem Linux mit Bluetooth), hält die BLE-Verbindung,
  gibt die Werte auf TCP-Port 51235 aus und nimmt dort neue Zieltemperaturen an. Mit Display zeigt es
  ein Vollbildfenster, solange Messwerte kommen.
- **`bbq_client.py`** ist das Fenster für den Rechner: beide Temperaturen, Klick auf eine Karte setzt das Ziel.
  Qt (PyQt6), folgt den Systemfarben hell/dunkel.

## Gateway auf dem Pi

```bash
git clone https://github.com/<user>/BBQ.git ~/bbq && cd ~/bbq
python3 -m venv venv && venv/bin/pip install -r requirements.txt
BBQ_SELFTEST=1 venv/bin/python3 bbq.py      # Selbsttest, ohne Thermometer
venv/bin/python3 bbq.py                     # braucht ein Display (DISPLAY=:0), siehe unten
```

Beim ersten Start zeigt das Display **„Thermometer koppeln“**: Thermometer einschalten, den gefundenen
Eintrag antippen. Die Adresse landet als `BBQ_MAC` in `bbq.env`, danach verbindet sich der Pi von selbst.
Neu koppeln: die Zeile `BBQ_MAC=…` aus `bbq.env` löschen und den Dienst neu starten.

Als Dienst: Pfade und `User=` in [`bbq.service`](bbq.service) anpassen, dann

```bash
sudo cp bbq.service /etc/systemd/system/ && sudo systemctl enable --now bbq
journalctl -u bbq -f
```

Nur eine App kann gleichzeitig mit dem Thermometer verbunden sein. Ist ein Handy verbunden, findet der Pi es nicht.

### Einstellungen

Alle optional, als Umgebungsvariable oder als `KEY=VALUE` in `bbq.env` neben `bbq.py` (nicht im Git):

| Variable | Standard | Bedeutung |
|---|---|---|
| `BBQ_NAME` | `Day_BBQ,Day_BQQ` | BLE-Namen, an denen das Thermometer erkannt wird (Werbung / nach dem Verbinden) |
| `BBQ_MAC` | – | Das gekoppelte Thermometer (z. B. `AA:BB:CC:DD:EE:FF`); setzt der Kopplungsdialog |
| `BBQ_PORT` | `51235` | TCP-Port |
| `BBQ_IDLE_IF` | – | Existiert dieser Pfad, ruht das Gateway (z. B. `/sys/class/net/can0`, wenn der Pi woanders gebraucht wird) |

## Bedienen

```bash
nc raspberrypi.local 51235      # Live-Zeile; "p1 80" + Enter setzt Ziel P1 auf 80 °C (1 … 249)
```

Fenster-App auf dem Rechner (braucht PyQt6, z. B. `sudo apt install python3-pyqt6`):

```bash
python3 bbq_client.py <pi-hostname>          # oder BBQ_HOST setzen; Standard raspberrypi.local
sed "s|@DIR@|$PWD|g; s|@HOST@|<pi-hostname>|" bbq-client.desktop.in > ~/.local/share/applications/bbq-client.desktop
kbuildsycoca6                                # nur KDE: sonst startet das Menü noch den alten Eintrag
```

Klick auf eine Karte öffnet das Ziel: Enter oder Klick daneben übernimmt, Esc verwirft.

## Protokoll

Herausgefunden aus der Android-App „lazy BBQ“ (`com.bbq.dc`) und am Gerät geprüft. Dienst `fe551580-4180-8a02-ef2c-1b42a0ac3f83`:

- **`…1582` (Notify):** 10 Bytes, `<4H` = Ist P1, Ist P2, Ziel P1, Ziel P2 in °C, dann 2 Bytes unbekannt
  (immer `a0 a0`, auch während das Gerät Alarm piept: Der Alarm wird nicht übertragen). Fühler ab: `0x7fff`.
- **`…1581` (Write without response):** `[Ziel P1, 0, Ziel P2, 0, 0, Summe der Bytes & 0xFF]`, ganze °C;
  `0xFA` heißt „diesen Fühler nicht ändern“. Beispiel Ziel P1 = 75 °C: `4b 00 fa 00 00 45`.

Das Logo liegt als PhotoCraft-Datei mit Ebenen in `assets/bbq-logo.pcraft`.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
