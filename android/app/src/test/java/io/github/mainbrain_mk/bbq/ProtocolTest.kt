package io.github.mainbrain_mk.bbq

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

// Dieselben Faelle wie der Selbsttest von bbq_client.py
class ProtocolTest {
    private val line = "\r16:21:08   P1  23 °C (Ziel 75)   P2  19 °C (Ziel 80)   "

    @Test fun parseLine() {
        val half = line.replace(" 23 ", "  24 ").take(20)
        val (r, rest) = parse("\nverbunden\n" + line + half)
        assertEquals(Reading(23, 19, 75, 80), r)
        assert(rest.endsWith(half)) // halbe Zeile bleibt fuer spaeter
        assertEquals(null to "\nverbunden\n", parse("\nverbunden\n"))
        assertNull(parse(parse(line).second + "\nnicht gefunden …\n").first) // alte Zeile zaehlt nicht doppelt
    }

    @Test fun hosts() {
        assertEquals(Host("car.local"), parseHost(" car.local "))
        assertEquals(Host("192.168.0.5", 4000), parseHost("192.168.0.5:4000"))
        assertEquals(Host("car.local", 51235), parseHost("tcp://car.local:51235/"))
        assertNull(parseHost(""))
        assertNull(parseHost("zwei worte"))
        assertNull(parseHost("a:b:c"))
        assertEquals("car.local", Host("car.local").toString())
        assertEquals("car.local:4000", Host("car.local", 4000).toString())
    }

    @Test fun blePackets() { // Beispiele aus der README
        fun bytes(vararg v: Int) = v.map { it.toByte() }.toByteArray()
        assertEquals(Reading(23, NO_PROBE, 75, 80), decode(bytes(0x17, 0, 0xff, 0x7f, 0x4b, 0, 0x50, 0, 0xa0, 0xa0)))
        assertNull(decode(bytes(0x17, 0, 0xff)))
        assert(bytes(0x4b, 0, 0xfa, 0, 0, 0x45).contentEquals(targetCommand(1, 75)))
        assert(bytes(0xfa, 0, 0x50, 0, 0, 0x4a).contentEquals(targetCommand(2, 80)))
    }

    @Test fun chartHelpers() {
        val h = listOf(0L to Reading(20, 30, 65, 65), 1L to Reading(21, NO_PROBE, 65, 65),
            2L to Reading(22, 31, 65, 65), 30L to Reading(40, 32, 70, 65)).map { Sample(it.first, it.second) }
        assertEquals(listOf(listOf(0L to 20, 1L to 21, 2L to 22), listOf(30L to 40)), series(h, 1, gap = 10))
        assertEquals(listOf(listOf(0L to 30), listOf(2L to 31), listOf(30L to 32)), series(h, 2, gap = 10))
        assertEquals(emptyList<List<Pair<Long, Int>>>(), series(emptyList(), 1))
        assertEquals(listOf(15, 20, 25), ticks(19, 23))
        assertEquals(listOf(0, 50, 100, 150, 200), ticks(20, 188))
        assertEquals(listOf(65, 70), ticks(65, 65))
    }
}
