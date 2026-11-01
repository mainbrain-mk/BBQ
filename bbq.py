"""Day_BBQ-Fleischthermometer: BLE -> WLAN (TLS) + Vollbildfenster auf dem Pi-Display.

Haelt die BLE-Verbindung zum Thermometer, schickt die Live-Zeile an jeden gekoppelten Rechner
(bbq_client.py) und legt ein Vollbildfenster ueber das Display, solange frische Werte kommen.

Netz: TLS mit selbstsigniertem Zertifikat (bbq.crt/bbq.key, beim ersten Start erzeugt). Daten und
Befehle nur nach "auth <token>"; ein Token gibt es per Kopplung: "pair <name>" zeigt einen 6-stelligen
Code auf dem Display (und im Journal), "code <123456>" liefert "paired <token>". Gekoppelte Rechner
stehen in bbq.clients (Hash, Datum, Name); Zeile loeschen = entkoppeln.

Beim ersten Start (kein BBQ_MAC) zeigt das Display "Thermometer koppeln": gefundene Thermometer
antippen, die Adresse landet in `bbq.env`. Einstellungen als Umgebungsvariablen oder in `bbq.env`
neben diesem Skript: BBQ_NAME, BBQ_MAC, BBQ_PORT, BBQ_IDLE_IF (siehe README).
"""
import asyncio, hashlib, hmac, os, re, secrets, ssl, struct, subprocess, threading, time
import tkinter as tk
from bleak import BleakClient, BleakScanner


def load_env(path):
    """KEY=VALUE-Zeilen (# = Kommentar) als Umgebungsvariablen, echte Umgebung hat Vorrang."""
    try:
        with open(path) as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and not k.startswith("#"):
                    os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


def save_env(path, key, value):
    """Setzt KEY=VALUE in der env-Datei: ersetzt eine vorhandene Zeile, sonst angehaengt."""
    try:
        with open(path) as f:
            lines = [l for l in f.read().splitlines() if l.partition("=")[0].strip() != key]
    except FileNotFoundError:
        lines = []
    with open(path, "w") as f:
        f.write("\n".join(lines + [f"{key}={value}"]) + "\n")


HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, "bbq.env")
CERT, KEY, CLIENTS = (os.path.join(HERE, n) for n in ("bbq.crt", "bbq.key", "bbq.clients"))
CODE_S, CODE_TRIES, LOCK_S = 120, 3, 30  # Kopplungscode: gueltig 2 min, 3 Versuche, danach 30 s Sperre
load_env(ENV_FILE)
# Werbung sagt "Day_BBQ", nach dem Verbinden heisst es "Day_BQQ" (GAP-Name): beide gelten
NAMES = os.environ.get("BBQ_NAME", "Day_BBQ,Day_BQQ").split(",")
MAC = os.environ.get("BBQ_MAC", "").upper()  # leer: Kopplungsdialog am Display
PORT = int(os.environ.get("BBQ_PORT", "51235"))
IDLE_IF = os.environ.get("BBQ_IDLE_IF", "")  # optional: existiert dieser Pfad, ruht BLE (z. B. /sys/class/net/can0)
NOTIFY = "fe551582-4180-8a02-ef2c-1b42a0ac3f83"
WRITE = "fe551581-4180-8a02-ef2c-1b42a0ac3f83"  # Zieltemperatur, Format aus der App "lazy BBQ"
STALE_S = 10  # so lange ohne Messung -> Fenster weg

clients = set()
last = ""  # letzte Temperaturzeile, bekommt jeder neue TCP-Client sofort
reading = None  # (p1, p2, t1, t2, time.monotonic())
ble = None  # verbundener BleakClient, solange die Verbindung steht
candidates = {}  # beim Koppeln gefundene Thermometer: Adresse -> (Name, RSSI)
pending = None  # laufende Rechner-Kopplung: [Code, Ablauf, Versuche uebrig, Name, writer]; nur eine zur Zeit
paired_shown = None  # (Name, bis wann) fuer "Gekoppelt: …" auf dem Display
locked_until = 0.0  # nach 3 falschen Codes keine neue Kopplung bis dahin
FPR = ""  # Fingerabdruck des Zertifikats (SHA-256, hex); die ersten 8 Zeichen zeigen Display und App beim Koppeln


def ensure_cert():
    """TLS-Kontext; das selbstsignierte Zertifikat entsteht beim ersten Start. Die App merkt sich beim
    Koppeln seinen Fingerabdruck und spricht danach nur mit genau diesem Pi."""
    if not (os.path.exists(CERT) and os.path.exists(KEY)):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-keyout", KEY, "-out", CERT, "-days", "36500", "-subj", "/CN=bbq"],
                       check=True, capture_output=True)
        os.chmod(KEY, 0o600)
    global FPR
    with open(CERT) as f:
        FPR = hashlib.sha256(ssl.PEM_cert_to_DER_cert(f.read())).hexdigest().upper()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT, KEY)
    return ctx


def clean(name):
    """Rechnername aus dem Netz: nur druckbare Zeichen, kurz, nie leer (landet auf Display und in Datei)."""
    return "".join(c for c in name if c.isprintable()).strip()[:40] or "unbekannt"


def known(token, path=CLIENTS):
    """Name des gekoppelten Rechners zu diesem Token, sonst None. Gespeichert ist nur der Hash."""
    h = hashlib.sha256(token.encode()).hexdigest()
    try:
        with open(path) as f:
            for line in f:
                parts = line.rstrip("\n").split(" ", 2)  # "<sha256> <datum> <name>"
                if len(parts) == 3 and hmac.compare_digest(parts[0], h):
                    return parts[2]
    except FileNotFoundError:
        pass
    return None


def add_client(name, path=CLIENTS):
    """Neuen Rechner eintragen, liefert sein Token (256 Bit, verlaesst den Pi nur einmal, per TLS)."""
    token = secrets.token_urlsafe(32)
    with open(path, "a") as f:
        f.write(f"{hashlib.sha256(token.encode()).hexdigest()} {time.strftime('%F')} {clean(name)}\n")
    os.chmod(path, 0o600)
    return token


def send(text: str, log=False):
    for w in clients:
        w.write(text.encode())
    if log:
        print(f"{time.strftime('%F %T')} {text.strip()}", flush=True)


def show(_, data: bytearray):
    global last, reading
    p1, p2, t1, t2 = struct.unpack("<4H", data[:8])  # °C: Ist P1, Ist P2, Ziel P1, Ziel P2
    reading = (p1, p2, t1, t2, time.monotonic())
    last = f"\r{time.strftime('%H:%M:%S')}   P1 {p1:3d} °C (Ziel {t1})   P2 {p2:3d} °C (Ziel {t2})   "
    send(last)


def command(line: bytes):
    """b"p1 80" -> Schreibbefehl fuer Ziel P1 = 80 °C, sonst None.

    [Ziel P1, 0, Ziel P2, 0, 0, Summe]; 0xFA heisst "Fuehler nicht aendern", daher max. 249 °C.
    """
    m = re.fullmatch(rb"\s*p([12])\s+(\d{1,3})\s*", line.lower())
    if not m or not 0 < int(m[2]) < 0xFA:
        return None
    t = int(m[2])
    b = [t, 0, 0xFA, 0, 0] if m[1] == b"1" else [0xFA, 0, t, 0, 0]
    return bytes(b + [sum(b) & 0xFF])


async def on_client(reader, writer):
    """Vor "auth" oder erfolgreicher Kopplung gibt es nur Protokoll-Antworten, keine Daten, keine Befehle."""
    global pending, paired_shown, locked_until
    who = None  # Name des angemeldeten Rechners

    def reply(text):
        writer.write(f"{text}\n".encode())

    def welcome(name):
        nonlocal who
        who = name
        clients.add(writer)
        reply("ok")
        if fresh(reading, time.monotonic(), False):  # alte Werte nicht als aktuelle ausgeben (Thermometer aus)
            writer.write(last.encode())

    reply("BBQ/2")
    try:
        while line := await reader.readline():  # bis der Client trennt
            word, _, arg = line.decode(errors="replace").strip().partition(" ")
            now = time.monotonic()
            if who:
                cmd = command(line)
                if not cmd:
                    reply("\nBefehl: p1 80 oder p2 80 (1 … 249 °C)")
                elif not ble:
                    reply("\nThermometer nicht verbunden")
                else:
                    await ble.write_gatt_char(WRITE, cmd, response=False)
                    send(f"\nZiel {line.decode().strip()} °C gesetzt ({who})\n", log=True)
            elif word == "auth":
                name = known(arg)
                if name:
                    welcome(name)
                else:
                    reply("denied")
            elif word == "pair":
                if now < locked_until:
                    reply("busy")
                    continue
                pending = [f"{secrets.randbelow(10 ** 6):06d}", now + CODE_S, CODE_TRIES, clean(arg), writer]
                print(f"{time.strftime('%F %T')} Kopplungscode fuer {pending[3]}: {pending[0]} (2 min gueltig)", flush=True)
                reply("code")
            elif word == "code" and pending and pending[4] is writer and now < pending[1]:
                if hmac.compare_digest(arg, pending[0]):
                    name, pending = pending[3], None
                    reply(f"paired {add_client(name)}")
                    paired_shown = (name, now + 8)
                    print(f"{time.strftime('%F %T')} Rechner gekoppelt: {name}", flush=True)
                    welcome(name)
                else:
                    pending[2] -= 1
                    if pending[2]:
                        reply(f"wrong {pending[2]}")
                    else:  # Code verbraucht, kurz Ruhe: so laesst sich der Code nicht durchprobieren
                        pending, locked_until = None, now + LOCK_S
                        reply("expired")
            elif word == "code":
                reply("expired")
            else:
                reply("denied")
    except Exception as e:  # Client weg, TLS-Abbruch oder BLE-Abbruch mitten im Schreiben
        print(f"{time.strftime('%F %T')} Client: {e!r}", flush=True)
    finally:
        clients.discard(writer)
        if pending and pending[4] is writer:  # Rechner weg: Code vom Display nehmen
            pending = None
        writer.close()


def idle():
    return bool(IDLE_IF) and os.path.exists(IDLE_IF)


def ours(line: str):
    """Zeile aus `bluetoothctl devices Connected` -> Adresse, wenn es unser Thermometer ist."""
    parts = line.split(" ", 2)  # "Device AA:BB:CC:DD:EE:FF Day_BQQ"
    if len(parts) == 3 and parts[0] == "Device" and (parts[1] == MAC if MAC else parts[2] in NAMES):
        return parts[1]
    return None


async def drop_orphans():
    """Verwaiste Verbindung trennen (z. B. Prozess mitten im Connect neu gestartet): Die haelt BlueZ
    offen, dann wirbt das Thermometer nicht mehr und der Scan findet es nie."""
    p = await asyncio.create_subprocess_exec("bluetoothctl", "devices", "Connected", stdout=asyncio.subprocess.PIPE)
    out, _ = await p.communicate()
    for addr in filter(None, map(ours, out.decode(errors="replace").splitlines())):
        p = await asyncio.create_subprocess_exec("bluetoothctl", "disconnect", addr, stdout=asyncio.subprocess.DEVNULL)
        await p.wait()


async def ble_main(tls):
    global ble, candidates
    await asyncio.start_server(on_client, port=PORT, ssl=tls)
    while True:  # Signal ist schwach: bei Abbruch einfach neu verbinden
        if idle():
            await asyncio.sleep(30)
            continue
        try:
            if not MAC:  # Erststart: nur suchen, bis am Display eins gewaehlt ist (verbinden wuerde es unsichtbar machen)
                await drop_orphans()
                found = await BleakScanner.discover(timeout=5, return_adv=True)
                candidates = {d.address: (adv.local_name or d.name, adv.rssi)
                              for d, adv in found.values() if (adv.local_name or d.name) in NAMES}
                continue
            dev = await BleakScanner.find_device_by_address(MAC, timeout=20)
            if not dev:
                send(f"\n{MAC} nicht gefunden (aus, ausser Reichweite oder Handy verbunden?), suche weiter …\n", log=True)
                await drop_orphans()
                continue
            gone = asyncio.Event()
            async with BleakClient(dev, timeout=20, disconnected_callback=lambda _: gone.set()) as c:
                send("\nverbunden\n", log=True)
                await c.start_notify(NOTIFY, show)
                ble = c
                try:
                    await gone.wait()
                finally:
                    ble = None
            send("\nVerbindung verloren, verbinde neu …\n", log=True)
        except Exception as e:
            send(f"\n{e!r}, neuer Versuch …\n", log=True)
            await asyncio.sleep(2)


BG, CARD, TEXT, DIM, GREEN = "#2c3a52", "#16191e", "#e8eaf4", "#666b78", "#2ecc52"


def fresh(r, now, idle_now):
    return r is not None and now - r[4] < STALE_S and not idle_now


def main():
    tls = ensure_cert()  # hier im Hauptthread: scheitert openssl, steht es im Journal und der Dienst startet neu
    threading.Thread(target=lambda: asyncio.run(ble_main(tls)), daemon=True).start()
    root = tk.Tk()
    root.title("BBQ")
    root.configure(bg=BG)
    cards = []
    for i in range(2):
        f = tk.Frame(root, bg=CARD)
        f.place(relx=0.01 + i * 0.5, rely=0.05, relwidth=0.48, relheight=0.9)
        tk.Label(f, text=f"TEMP P{i + 1}", font=("DejaVu Sans", 22, "bold"), fg=DIM, bg=CARD).pack(anchor="w", padx=20, pady=10)
        row = tk.Frame(f, bg=CARD)
        row.pack(expand=True)
        # Zahl doppelt so gross wie frueher, "°C" klein daneben: so passen auch 3 Stellen (Ofen, Smoker) in die Kachel
        ist = tk.Label(row, font=("DejaVu Sans", 240, "bold"), fg=TEXT, bg=CARD)
        ist.pack(side="left")
        unit = tk.Label(row, text="°C", font=("DejaVu Sans", 80, "bold"), fg=TEXT, bg=CARD)
        unit.pack(side="left", anchor="n", pady=(60, 0))
        ziel = tk.Label(f, font=("DejaVu Sans", 40, "bold"), fg=DIM, bg=CARD)
        ziel.pack(pady=30)
        cards.append((ist, unit, ziel))

    # Kopplungsdialog (nur ohne BBQ_MAC): liegt ueber den Karten, bis ein Thermometer angetippt ist
    pair = tk.Frame(root, bg=BG)
    pair.place(relx=0, rely=0, relwidth=1, relheight=1)
    tk.Label(pair, text="Thermometer koppeln", font=("DejaVu Sans", 34, "bold"), fg=TEXT, bg=BG).pack(pady=(30, 6))
    tk.Label(pair, text="Thermometer einschalten und in die Nähe stellen.\nIst es mit einem Handy verbunden, dort trennen.",
             font=("DejaVu Sans", 16), fg=DIM, bg=BG).pack()
    found = tk.Frame(pair, bg=BG)
    found.pack(fill="x", padx=40, pady=20)
    status = tk.Label(pair, font=("DejaVu Sans", 16), fg=DIM, bg=BG)
    status.pack(side="bottom", pady=20)
    buttons = {}  # Adresse -> Button

    def choose(addr):
        global MAC
        save_env(ENV_FILE, "BBQ_MAC", addr)
        MAC = addr
        send(f"\nThermometer {addr} gekoppelt\n", log=True)
        root.grab_release()
        pair.destroy()

    def on_tap(e):
        # Mit dem Grab kommt der Tipp beim Fenster an, nicht beim Knopf darunter; winfo_containing
        # sieht unter Xwayland das fremde X-Fenster, also selbst nachsehen, welcher Knopf getroffen ist
        if e.widget is not root or MAC:
            return
        for b in buttons.values():
            x, y = b.winfo_rootx(), b.winfo_rooty()
            if x <= e.x_root < x + b.winfo_width() and y <= e.y_root < y + b.winfo_height():
                b.invoke()
                return

    root.bind("<ButtonRelease-1>", on_tap)
    if MAC:  # schon gekoppelt: Dialog gar nicht erst zeigen, er laege sonst ueber den Karten
        pair.destroy()

    # Rechner-Kopplung: Code gross ueber allem, danach kurz "Gekoppelt: …"
    codeview = tk.Frame(root, bg=BG)
    code_head = tk.Label(codeview, font=("DejaVu Sans", 34, "bold"), fg=TEXT, bg=BG)
    code_head.pack(pady=(60, 10))
    code_big = tk.Label(codeview, font=("DejaVu Sans", 160, "bold"), fg=TEXT, bg=BG)
    code_big.pack(expand=True)
    code_foot = tk.Label(codeview, font=("DejaVu Sans", 20), fg=DIM, bg=BG)
    code_foot.pack(pady=40)
    root.withdraw()

    def overlay(now):
        """Zeigt Kopplungscode bzw. Bestaetigung; True, solange etwas davon sichtbar sein soll."""
        p, done = pending, paired_shown
        if p and now < p[1]:
            code_head.config(text=f"Kopplung mit „{p[3]}“", fg=TEXT)
            code_big.config(text=f"{p[0][:3]} {p[0][3:]}", fg=TEXT)
            code_foot.config(text=f"Diesen Code in der BBQ-App eingeben · Pi-Kennung {FPR[:4]} {FPR[4:8]}"
                                  f" (muss in der App gleich sein) · noch {int(p[1] - now)} s")
        elif done and now < done[1]:
            code_head.config(text="Gekoppelt", fg=GREEN)
            code_big.config(text="✓", fg=GREEN)
            code_foot.config(text=done[0])
        else:
            codeview.place_forget()
            return False
        codeview.place(relx=0, rely=0, relwidth=1, relheight=1)
        codeview.lift()
        return True

    def tick():
        r, pairing = reading, not MAC
        showing_code = overlay(time.monotonic())
        if pairing:
            c = candidates
            if sorted(c) != sorted(buttons):
                for b in buttons.values():
                    b.destroy()
                buttons.clear()
                for addr in sorted(c, key=lambda a: -(c[a][1] or -999)):  # staerkstes Signal oben
                    buttons[addr] = tk.Button(found, font=("DejaVu Sans", 22, "bold"), fg=TEXT, bg=CARD, activeforeground=TEXT,
                                              activebackground=GREEN, relief="flat", pady=18, command=lambda a=addr: choose(a))
                    buttons[addr].pack(fill="x", pady=6)
            for addr, b in buttons.items():
                name, rssi = c.get(addr, ("?", None))
                b.config(text=f"{name}   {addr}   {rssi} dBm")
            status.config(text="Antippen zum Koppeln" if c else "Suche …")
            if root.state() != "withdrawn" and not root.grab_current():
                # Unter Xwayland landen Tipps sonst im X-Fenster darunter (z. B. einer anderen Vollbild-App)
                try:
                    root.grab_set_global()
                except tk.TclError:  # Fenster noch nicht sichtbar oder Grab belegt: naechster Tick
                    pass
        elif fresh(r, time.monotonic(), idle()):
            for (ist, unit, ziel), p, t in zip(cards, r[:2], r[2:4]):
                ist.config(text=f"{p}", fg=GREEN if t and p >= t else TEXT)
                unit.config(fg=GREEN if t and p >= t else TEXT)
                ziel.config(text=f"Ziel {t} °C")
        if pairing or showing_code or fresh(r, time.monotonic(), idle()):
            if root.state() == "withdrawn":
                root.deiconify()
                root.attributes("-fullscreen", True)
                root.attributes("-topmost", True)
        elif root.state() != "withdrawn":
            root.withdraw()
        root.after(500, tick)

    tick()
    root.mainloop()


if __name__ == "__main__":
    if os.environ.get("BBQ_SELFTEST"):
        assert fresh((80, 54, 75, 90, 100.0), 105.0, False)
        assert not fresh((80, 54, 75, 90, 100.0), 100.0 + STALE_S, False)  # zu alt
        assert not fresh((80, 54, 75, 90, 100.0), 101.0, True)  # BBQ_IDLE_IF existiert
        assert not fresh(None, 1.0, False)
        NAMES, MAC = ["Day_BBQ", "Day_BQQ"], ""  # Standard: Thermometer am Namen erkennen
        assert ours("Device AA:BB:CC:DD:EE:FF Day_BQQ") == "AA:BB:CC:DD:EE:FF"
        assert ours("Device AA:BB:CC:DD:EE:FF Day_BBQ") == "AA:BB:CC:DD:EE:FF"
        assert ours("Device 00:11:22:33:44:55 Kopfhoerer") is None and ours("") is None
        MAC = "00:11:22:33:44:55"  # mit BBQ_MAC zaehlt nur die Adresse
        assert ours("Device AA:BB:CC:DD:EE:FF Day_BQQ") is None and ours("Device 00:11:22:33:44:55 x") == MAC
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# Kommentar\nBBQ_TEST_A = 1\nBBQ_TEST_B=x=y\nkaputt\n")
        load_env(f.name)
        os.unlink(f.name)
        assert os.environ["BBQ_TEST_A"] == "1" and os.environ["BBQ_TEST_B"] == "x=y"
        save_env(f.name, "BBQ_MAC", "AA:BB:CC:DD:EE:FF")  # Datei fehlt -> neu
        with open(f.name, "a") as g:
            g.write("BBQ_IDLE_IF=/x\n")
        save_env(f.name, "BBQ_MAC", "11:22:33:44:55:66")  # ersetzt, Rest bleibt
        with open(f.name) as g:
            assert g.read() == "BBQ_IDLE_IF=/x\nBBQ_MAC=11:22:33:44:55:66\n"
        os.unlink(f.name)
        clients_file = f.name + ".clients"
        assert known("x", clients_file) is None  # Datei fehlt
        token = add_client("Rechner\nmit Umbruch", clients_file)
        assert known(token, clients_file) == "Rechnermit Umbruch" and known(token + "x", clients_file) is None
        with open(clients_file) as g:
            assert token not in g.read()  # nur der Hash liegt auf dem Pi
        assert os.stat(clients_file).st_mode & 0o077 == 0
        os.unlink(clients_file)
        assert clean("") == "unbekannt" and len(clean("x" * 99)) == 40
        assert command(b"p1 75\n") == bytes.fromhex("4b00fa000045")  # am Geraet geprueft
        assert command(b"P2 80\r\n") == bytes.fromhex("fa005000004a")
        assert command(b"p1 250") is None and command(b"p3 80") is None and command(b"p1 0") is None
        print("ok")
    else:
        main()
