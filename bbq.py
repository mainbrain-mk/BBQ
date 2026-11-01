"""Day_BBQ-Grillthermometer: BLE -> WLAN (TCP) + Vollbildfenster auf dem Pi-Display.

Haelt die BLE-Verbindung zum Thermometer, schickt die Live-Zeile an jeden TCP-Client und legt ein
Vollbildfenster ueber das Display, solange frische Werte kommen. Vom Rechner aus:

    nc <pi>.local 51235    # dort "p1 80" + Enter setzt Ziel P1 auf 80 °C

Beim ersten Start (kein BBQ_MAC) zeigt das Display "Thermometer koppeln": gefundene Thermometer
antippen, die Adresse landet in `bbq.env`. Einstellungen als Umgebungsvariablen oder in `bbq.env`
neben diesem Skript: BBQ_NAME, BBQ_MAC, BBQ_PORT, BBQ_IDLE_IF (siehe README).
"""
import asyncio, os, re, struct, threading, time
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


ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bbq.env")
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
    clients.add(writer)
    writer.write(last.encode())
    try:
        while line := await reader.readline():  # bis der Client trennt
            cmd = command(line)
            if not cmd:
                writer.write("\nBefehl: p1 80 oder p2 80 (1 … 249 °C)\n".encode())
            elif not ble:
                writer.write("\nThermometer nicht verbunden\n".encode())
            else:
                await ble.write_gatt_char(WRITE, cmd, response=False)
                send(f"\nZiel {line.decode().strip()} °C gesetzt\n", log=True)
    except Exception as e:  # Client weg oder BLE-Abbruch mitten im Schreiben
        print(f"{time.strftime('%F %T')} Client: {e!r}", flush=True)
    finally:
        clients.discard(writer)
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


async def ble_main():
    global ble, candidates
    await asyncio.start_server(on_client, port=PORT)
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
    threading.Thread(target=lambda: asyncio.run(ble_main()), daemon=True).start()
    root = tk.Tk()
    root.title("Grill")
    root.configure(bg=BG)
    cards = []
    for i in range(2):
        f = tk.Frame(root, bg=CARD)
        f.place(relx=0.01 + i * 0.5, rely=0.05, relwidth=0.48, relheight=0.9)
        tk.Label(f, text=f"TEMP P{i + 1}", font=("DejaVu Sans", 22, "bold"), fg=DIM, bg=CARD).pack(anchor="w", padx=20, pady=10)
        ist = tk.Label(f, font=("DejaVu Sans", 120, "bold"), fg=TEXT, bg=CARD)
        ist.pack(expand=True)
        ziel = tk.Label(f, font=("DejaVu Sans", 40, "bold"), fg=DIM, bg=CARD)
        ziel.pack(pady=30)
        cards.append((ist, ziel))

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
    root.withdraw()

    def tick():
        r, pairing = reading, not MAC
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
            for (ist, ziel), p, t in zip(cards, r[:2], r[2:4]):
                ist.config(text=f"{p} °C", fg=GREEN if t and p >= t else TEXT)
                ziel.config(text=f"Ziel {t} °C")
        if pairing or fresh(r, time.monotonic(), idle()):
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
        assert command(b"p1 75\n") == bytes.fromhex("4b00fa000045")  # am Geraet geprueft
        assert command(b"P2 80\r\n") == bytes.fromhex("fa005000004a")
        assert command(b"p1 250") is None and command(b"p3 80") is None and command(b"p1 0") is None
        print("ok")
    else:
        main()
