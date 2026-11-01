# Day_BBQ-Fleischthermometer im Netz

<img src="assets/bbq-logo.png" width="96" align="right" alt="Logo: gekreuzte Grillzange und Fleischgabel">

Für das Bluetooth-Fleischthermometer „Day_BBQ“ (Boomex, Artikel 57675, meldet sich per BLE als `Day_BBQ` bzw. `Day_BQQ`)
gibt es keine funktionierende Android-App mehr. Dieses Projekt ersetzt sie:

- **`bbq.py`** läuft auf einem Raspberry Pi (oder jedem Linux mit Bluetooth), hält die BLE-Verbindung,
  gibt die Werte verschlüsselt (TLS, Port 51235) an gekoppelte Rechner aus und nimmt von ihnen neue
  Zieltemperaturen an. Mit Display zeigt es ein Vollbildfenster, solange Messwerte kommen.
- **`bbq_client.py`** ist das Fenster für den Rechner: beide Temperaturen, Klick auf eine Karte setzt das Ziel.
  Qt (PyQt6), folgt den Systemfarben hell/dunkel.
- **`android/`** ist dieselbe App fürs Handy (Kotlin, Jetpack Compose, ab Android 8), über den Pi oder direkt per
  Bluetooth, dazu ein Hintergrunddienst,
  der die Werte als Benachrichtigung zeigt und Alarm gibt, wenn ein Fühler sein Ziel erreicht.

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
| `BBQ_PORT` | `51235` | TCP-Port (TLS) |
| `BBQ_IDLE_IF` | – | Existiert dieser Pfad, ruht das Gateway (z. B. `/sys/class/net/can0`, wenn der Pi woanders gebraucht wird) |

## Bedienen

Fenster-App auf dem Rechner (braucht PyQt6, z. B. `sudo apt install python3-pyqt6`):

```bash
python3 bbq_client.py                        # beim ersten Start: Pi koppeln
sed "s|@DIR@|$PWD|g" bbq-client.desktop.in > ~/.local/share/applications/bbq-client.desktop
kbuildsycoca6                                # nur KDE: sonst startet das Menü noch den alten Eintrag
```

Klick auf eine Karte öffnet das Ziel: Enter oder Klick daneben übernimmt, Esc verwirft.

### Rechner koppeln

Nur gekoppelte Rechner bekommen Messwerte und dürfen Ziele setzen.

1. In der App die Adresse des Pi eintragen (z. B. `raspberrypi.local`, `192.168.0.10` oder `host:51235`)
   und **Koppeln** klicken.
2. Der Pi zeigt einen 6-stelligen Code auf dem Display (ohne Display: `journalctl -u bbq`), 2 Minuten
   gültig, 3 Versuche, danach 30 Sekunden Sperre.
3. Die **Pi-Kennung** in der App muss mit der auf dem Display übereinstimmen, dann den Code eingeben.

Danach stehen Adresse, Token und Zertifikat-Fingerabdruck des Pi in `~/.config/bbq/bbq.env` (nur für den
Besitzer lesbar), und die App verbindet sich beim nächsten Start von selbst, nur mit genau diesem Pi.
Auf dem Pi stehen gekoppelte Rechner in `bbq.clients` (nur ein Hash des Tokens, Datum, Name): Zeile löschen
entkoppelt den Rechner. `bbq.crt`/`bbq.key` entstehen beim ersten Start; werden sie gelöscht, müssen alle
Rechner neu koppeln.

### Android

Bauen braucht JDK 17 und das Android SDK (Plattform 36), kein Android Studio:

```bash
cd android && ANDROID_HOME=~/Android/Sdk ./gradlew assembleDebug testDebugUnitTest
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Die App folgt der Systemsprache (ab Android 13 auch pro App unter Einstellungen → Apps → BBQ → Sprache):
Englisch (Standard, `app/src/main/res/values/strings.xml`), Deutsch, Französisch, Spanisch, Italienisch,
Niederländisch, Polnisch, Portugiesisch, Tschechisch und Slowakisch (`values-<code>/`). Eine weitere Sprache:
`values-<code>/strings.xml` mit denselben Namen anlegen, Store-Texte unter `fastlane/metadata/android/<locale>/`.

Koppeln wie am Rechner (Adresse, **Koppeln**, Pi-Kennung vergleichen, Code). Danach hält ein Dienst die Verbindung
auch bei ausgeschaltetem Bildschirm: Die Benachrichtigung zeigt beide Werte, erreicht ein Fühler sein Ziel, kommt
ein Alarm mit Ton und Vibration (erneut erst, wenn er 2 °C darunter war). Dafür bleiben CPU und WLAN wach, solange
der Dienst läuft; **Beenden** in der Benachrichtigung trennt und gibt beides frei. Klappt `<name>.local` auf dem
Handy nicht, die IP-Adresse des Pi eintragen.

**Ohne Pi:** In der Einrichtung unter „Ohne Pi“ **Thermometer suchen** (die App fragt nach dem Bluetooth-Recht,
bis Android 11 nach dem Standort, der dann auch eingeschaltet sein muss) und den gefundenen Eintrag antippen. Das Handy
hält dann selbst die BLE-Verbindung, alles andere (Verlauf, Ziele, Alarm) bleibt gleich; das WLAN muss dafür nicht
wach bleiben. Der Pi muss dazu aus sein (oder ruhen), sonst ist das Thermometer schon verbunden. Kommen keine Messwerte,
führt **Ändern** neben der Statuszeile zurück zur Einrichtung: dort mit derselben Pi-Adresse wieder auf **Koppeln**
(ein gekoppelter Pi braucht keinen neuen Code) oder **Zurück** zur bisherigen Verbindung.

### Signierte APK und F-Droid

Einmalig einen Release-Schlüssel anlegen, **außerhalb des Repos** aufheben und sichern: Updates müssen immer mit
demselben Schlüssel signiert sein, sonst lassen sie sich nicht über die installierte App spielen.

```bash
keytool -genkeypair -v -keystore ~/schluessel/bbq-release.jks -alias bbq -keyalg RSA -keysize 4096 -validity 10000
```

`android/keystore.properties.example` als `android/keystore.properties` kopieren (steht in `.gitignore`) und Pfad
und Passwörter eintragen. Dann:

```bash
cd android && ./gradlew clean assembleRelease
apksigner verify --print-certs app/build/outputs/apk/release/app-release.apk
```

(`apksigner` liegt in `$ANDROID_HOME/build-tools/<version>/`.)

Ohne `keystore.properties` entsteht `app-release-unsigned.apk`.

**Versionen:** Start ist 1.0. Ein Fehlerbehebungs-Update zählt +0.01 (1.0 → 1.01 → 1.02), ein kleines
Funktions-Update springt auf das nächste Zehntel (1.02 → 1.1, dann 1.11, 1.12 …); zehn Fixes in Folge ergeben
ebenso das nächste Zehntel (1.09 → 1.1). `versionCode` ist immer `versionName` × 100 (1.0 → 100, 1.01 → 101,
1.1 → 110, 1.12 → 112), so steigt er mit jeder Version. Für ein Release beide Werte in
`android/app/build.gradle.kts` setzen, `fastlane/metadata/android/*/changelogs/<versionCode>.txt` anlegen und den
Stand als `v<versionName>` taggen (z. B. `git tag v1.01 && git push origin v1.01`).

Store-Texte, Icon und Screenshot liegen im Fastlane-Format unter [`fastlane/metadata/android`](fastlane/metadata/android).
Die lesen F-Droid und IzzyOnDroid von selbst.

- **IzzyOnDroid** (schnell): die signierte APK als Asset an ein GitHub-Release zum Tag hängen, dann die Aufnahme
  beantragen (Anleitung auf apt.izzysoft.de/fdroid). Izzy übernimmt künftige Releases automatisch.
- **F-Droid** (offizielles Repo): F-Droid baut selbst aus dem Quellcode und signiert mit eigenem Schlüssel. Die
  Vorlage [`android/fdroid/io.github.mainbrain_mk.bbq.yml`](android/fdroid/io.github.mainbrain_mk.bbq.yml) als
  Merge Request in [fdroiddata](https://gitlab.com/fdroid/fdroiddata) einreichen. Neue Tags erkennt F-Droid dann
  selbst.

## Protokoll

Herausgefunden aus der Android-App „lazy BBQ“ (`com.bbq.dc`) und am Gerät geprüft. Dienst `fe551580-4180-8a02-ef2c-1b42a0ac3f83`:

- **`…1582` (Indicate, kein Notify):** 10 Bytes, `<4H` = Ist P1, Ist P2, Ziel P1, Ziel P2 in °C, dann 2 Bytes unbekannt
  (immer `a0 a0`, auch während das Gerät Alarm piept: Der Alarm wird nicht übertragen). Fühler ab: `0x7fff`.
- **`…1581` (Write without response):** `[Ziel P1, 0, Ziel P2, 0, 0, Summe der Bytes & 0xFF]`, ganze °C;
  `0xFA` heißt „diesen Fühler nicht ändern“. Beispiel Ziel P1 = 75 °C: `4b 00 fa 00 00 45`.

Zwischen Pi und App: TLS, zeilenweise. Der Pi grüßt mit `BBQ/2`; dann `auth <token>` → `ok`/`denied`,
oder `pair <name>` → `code`, `code <123456>` → `paired <token>` + `ok` (sonst `wrong <n>`, `expired`, `busy`).
Angemeldet kommen Live-Zeilen (`\rHH:MM:SS P1 … °C (Ziel …) P2 …`) und Meldungen; `p1 80` setzt ein Ziel.
Zum Ausprobieren: `openssl s_client -quiet -connect <pi>:51235`.

Das Logo liegt als PhotoCraft-Datei mit Ebenen in `assets/bbq-logo.pcraft`.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
