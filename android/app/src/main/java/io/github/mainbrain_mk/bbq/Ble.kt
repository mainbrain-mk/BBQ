package io.github.mainbrain_mk.bbq

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.content.Context
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import java.util.UUID

/** Direkt zum Thermometer per Bluetooth LE, ohne Pi; dieselben Kennungen wie bbq.py (README, Protokoll).
 *  Nur ein Geraet kann verbunden sein: Haelt der Pi das Thermometer, findet das Handy es nicht. */
@SuppressLint("MissingPermission") // die Rechte holt die Einrichtung vor dem Suchen
object Ble {
    private val SERVICE = UUID.fromString("fe551580-4180-8a02-ef2c-1b42a0ac3f83")
    private val NOTIFY = UUID.fromString("fe551582-4180-8a02-ef2c-1b42a0ac3f83")
    private val WRITE = UUID.fromString("fe551581-4180-8a02-ef2c-1b42a0ac3f83")
    private val CCCD = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb") // Notify an/aus
    private const val TAG = "bbq"
    private val main = Handler(Looper.getMainLooper())
    @Volatile private var gen = 0 // jede neue Verbindung zaehlt hoch, Rueckmeldungen der alten verfallen dann
    @Volatile private var gatt: BluetoothGatt? = null
    private var retry: Runnable? = null
    private var scan: Pair<BluetoothLeScanner, ScanCallback>? = null

    private fun adapter(ctx: Context) = ctx.getSystemService(BluetoothManager::class.java)?.adapter?.takeIf { it.isEnabled }

    /** 10 s nach Thermometern suchen: found(Adresse, Name, RSSI) je Treffer, danach done(). false: Bluetooth aus. */
    fun scan(ctx: Context, found: (String, String, Int) -> Unit, done: () -> Unit): Boolean {
        val sc = adapter(ctx)?.bluetoothLeScanner ?: return false
        stopScan()
        lateinit var cb: ScanCallback
        val end = Runnable {
            if (scan?.second === cb) {
                stopScan()
                done()
            }
        }
        cb = object : ScanCallback() {
            override fun onScanResult(type: Int, r: ScanResult) {
                val name = r.scanRecord?.deviceName ?: r.device.name
                if (name in NAMES) found(r.device.address, name, r.rssi)
            }

            override fun onScanFailed(code: Int) = end.run()
        }
        scan = sc to cb
        sc.startScan(cb)
        main.postDelayed(end, 10_000)
        return true
    }

    fun stopScan() {
        val (sc, cb) = scan ?: return
        scan = null
        try {
            sc.stopScan(cb)
        } catch (_: IllegalStateException) { // Bluetooth inzwischen aus
        }
    }

    /** Verbinden und halten wie bbq.py: Abbruch oder nicht gefunden -> nach 3 s neuer Versuch, bis close(). */
    fun connect(ctx: Context, mac: String, reading: (Reading) -> Unit, status: (String) -> Unit) {
        close()
        val my = ++gen
        var was = false // schon einmal verbunden: "verloren" statt "nicht gefunden"
        lateinit var attempt: Runnable
        fun again() {
            if (my != gen) return
            status(if (was) ctx.getString(R.string.ble_lost) else ctx.getString(R.string.ble_not_found, mac))
            main.postDelayed(attempt, 3000)
        }
        val cb = object : BluetoothGattCallback() {
            override fun onConnectionStateChange(g: BluetoothGatt, st: Int, state: Int) {
                Log.i(TAG, "Verbindung: Status $st, Zustand $state")
                if (my != gen) return g.close()
                if (state == BluetoothProfile.STATE_CONNECTED) {
                    was = true
                    status(ctx.getString(R.string.ble_connected))
                    g.discoverServices()
                } else {
                    g.close()
                    gatt = null
                    again()
                }
            }

            override fun onServicesDiscovered(g: BluetoothGatt, st: Int) {
                val ch = g.getService(SERVICE)?.getCharacteristic(NOTIFY)
                val d = ch?.getDescriptor(CCCD)
                if (my != gen || d == null) return g.disconnect()
                g.setCharacteristicNotification(ch, true)
                // das Thermometer kann nur Indicate (Eigenschaften 0x22), "Notify an" lehnt es mit Status 128 ab;
                // wie bleak: Notify, wenn angeboten, sonst Indicate
                val on = if (ch.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY != 0)
                    BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE else BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
                if (Build.VERSION.SDK_INT >= 33) g.writeDescriptor(d, on)
                else @Suppress("DEPRECATION") g.writeDescriptor(d.apply { value = on })
            }

            override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, st: Int) {
                Log.i(TAG, "Messwerte an: Status $st")
                if (my != gen) return
                if (st == BluetoothGatt.GATT_SUCCESS) status("") else g.disconnect() // abgelehnt: neu verbinden
            }

            override fun onCharacteristicChanged(g: BluetoothGatt, ch: BluetoothGattCharacteristic, value: ByteArray) {
                if (my == gen) decode(value)?.let(reading)
            }

            @Deprecated("bis Android 12; ab 13 kommt auch die Fassung mit value, daher dort hier nichts tun")
            override fun onCharacteristicChanged(g: BluetoothGatt, ch: BluetoothGattCharacteristic) {
                @Suppress("DEPRECATION")
                if (Build.VERSION.SDK_INT < 33) onCharacteristicChanged(g, ch, ch.value)
            }
        }
        attempt = Runnable {
            if (my != gen) return@Runnable
            val a = adapter(ctx)
            if (a == null) {
                status(ctx.getString(R.string.bt_off))
                main.postDelayed(attempt, 3000)
                return@Runnable
            }
            Log.i(TAG, "verbinde mit $mac")
            try {
                gatt = a.getRemoteDevice(mac).connectGatt(ctx, false, cb, BluetoothDevice.TRANSPORT_LE) ?: return@Runnable again()
            } catch (_: SecurityException) { // Recht in den Einstellungen entzogen
                status(ctx.getString(R.string.ble_revoked))
            }
        }
        retry = attempt
        attempt.run()
    }

    fun close() {
        gen++
        retry?.let { main.removeCallbacks(it) }
        retry = null
        gatt?.close()
        gatt = null
    }

    @Suppress("DEPRECATION")
    fun write(b: ByteArray) {
        val g = gatt ?: return
        val ch = g.getService(SERVICE)?.getCharacteristic(WRITE) ?: return
        if (Build.VERSION.SDK_INT >= 33) g.writeCharacteristic(ch, b, BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE)
        else {
            ch.writeType = BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
            ch.value = b
            g.writeCharacteristic(ch)
        }
    }
}
