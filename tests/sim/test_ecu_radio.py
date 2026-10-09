"""The ECU's radio telemetry, through the nRF24L01+ on its backplane (#193).

The radio is the vHIL's model (models/renode/Nrf24l01p.cs, after the nRF24L01+
Product Specification v1.0): an SPI slave on the GPIOs the ECU bit-bangs, which
sets TX_DS and pulls IRQ low 130 us of PLL settling plus the packet's time on
air after a CE pulse of at least 10 us. Before it, the pins floated, STATUS
read 0x00, and every send waited out its 10 ms timeout
(isc-fs/IFS08-CE-ECU#258); these tests hold the firmware to the success path.

Firmware facts (IFS08-CE-ECU dev 544b651):
  radio set up once, by TelemetryTask: EN_AA 0, SETUP_AW 5 bytes,
  SETUP_RETR 0, RF_CH 76, RF_SETUP 0x06 (1 Mbps, 0 dBm), TX_ADDR "ECU01",
  CONFIG EN_CRC | PWR_UP (1-byte CRC, PTX)           nrf24.c:195-289,
                                                      telemetry_task.cpp:307-308
  each 200 ms cycle (osDelayUntil(tick += PeriodMs)), seq += 1 from 1:
  send_dashboard(v, seq), then send_radio_snapshot   telemetry_task.cpp:35, :300,
                                                      :312, :322-326
  the snapshot: 102 bytes in 5 fragments of 32,
  each sent by Telemetry_Send32 =
  NRF24_SendPayload(p, 32, 10 ms), then
  osDelay(RadioFragmentPaceMs = 5)                   telemetry_task.cpp:23-25,
                                                      :40, :231-245;
                                                      radio_snapshot.hpp:27-30
  fragment: [0] 0xEC [1] 0x03 [2] frag_idx 0..4 [3] frag_tot 5
  [4..5] seq LE [6] kind 0x06 [7] 0
  [8..31] snapshot[frag_idx * 24 ..+24), the last zero-padded
                                                      radio_snapshot.hpp:16-19,
                                                      radio_snapshot.cpp:76-94
  snapshot: [0..3] tick_ms LE (osKernelGetTickCount at the send),
  [4..5] seq LE, [15] reserved 0, [96..101] reserved 0
                                                      radio_snapshot.cpp:32-74,
                                                      telemetry_task.cpp:323
  NRF24_SendPayload: payload, CE pulse (800 loops), NRF24_WaitIrqAssert
  (polls STATUS until TX_DS/MAX_RT/RX_DR or the timeout), then clears
  the flags by writing 1s to STATUS                  nrf24.c:576-642, :724-795
  0x510 d[6..7] = the cycle's seq; 0x51B d[4..7] = osKernelGetTickCount()
  in send_dashboard                                   telemetry_task.cpp:77, :136

Found here: none. What the model leaves out (no receiver, ACK or MAX_RT; the
radio's registers survive an MCU-only reset on the car) is in its header.
"""
import pytest

from cpu_timing import mips
from vhil.sim import assert_cadence

PERIOD_US = 200_000                       # telemetry_task.cpp:35 PeriodMs
FRAGMENTS, FRAGMENT_SIZE, SLICE = 5, 32, 24
SNAPSHOT_SIZE = 102
SEND_TIMEOUT_US = 10_000                  # telemetry_task.cpp:24
PACE_US = 5_000                           # telemetry_task.cpp:40
KERNEL_TICK_US = 1000
RUN_MS = 2000
# One bit-banged SCK period, NRF24_BitBangTransfer's three delays and pin
# accesses: 2875 + 5692 instructions in ECU dev 2026-10-09
# (test_ecu_cpu_timing.py).
BIT_INSTRUCTIONS = 8600


@pytest.fixture(scope="module")
def run(make_sim):
    sim = make_sim("ecu")
    t0 = sim.app_started["ecu"]
    sim.run_for(ms=RUN_MS)
    radio = sim.radio("radio")
    return sim, radio, radio.payloads(since_us=t0)


def _cycles(payloads):
    """seq -> its fragments, in the order sent."""
    out = {}
    for p in payloads:
        out.setdefault(int.from_bytes(p.data[4:6], "little"), []).append(p)
    return out


def test_the_radio_is_set_up_as_the_firmware_writes_it(run):
    sim, radio, _ = run
    regs = {name: sim.call(radio.path, "Register", reg).strip()
            for name, reg in (("CONFIG", 0x00), ("EN_AA", 0x01), ("SETUP_AW", 0x03),
                              ("SETUP_RETR", 0x04), ("RF_CH", 0x05), ("RF_SETUP", 0x06),
                              ("TX_ADDR", 0x10))}
    assert regs == {"CONFIG": "0a", "EN_AA": "00", "SETUP_AW": "03", "SETUP_RETR": "00",
                    "RF_CH": "4c", "RF_SETUP": "06", "TX_ADDR": b"ECU01".hex()}
    # 130 us settle + (1 preamble + 5 address + 32 payload + 1 CRC) bytes at
    # 1 Mbps, no packet control field with EN_AA = ARC = 0 (PS 7.10).
    assert float(sim.call(radio.path, "TxTimeUs")) == pytest.approx(130 + 8 * 39)


def test_every_send_ends_on_tx_ds_not_the_timeout(run):
    """NRF24_WaitIrqAssert returns on TX_DS: the firmware clears it (nrf24.c:622)
    within a STATUS poll or two of the packet's end, never the 10 ms
    timeout later. Every CE pulse is long enough to send (Thce 10 us)."""
    sim, radio, payloads = run
    assert len(payloads) >= FRAGMENTS * (RUN_MS * 1000 // PERIOD_US - 1)
    assert radio.count("ShortCePulses") == 0
    cleared = dict(radio.tx_ds_cleared(since_us=payloads[0].t_us))
    # A poll is a 2-byte bit-banged transfer, as fast as the core: a poll or
    # two, then the clearing write (docs/cpu-timing.md).
    poll_us = 16 * BIT_INSTRUCTIONS / mips(sim, "ecu")
    for p in payloads[:-1]:
        assert p.t_us in cleared, f"TX_DS at {p.t_us} us never cleared"
        assert cleared[p.t_us] - p.t_us < 3 * poll_us, (p.t_us, cleared[p.t_us], poll_us)
    # Back to back in a cycle: the 5 ms pace, then a send's bit-banged bytes
    # (nrf24.c:576-642: STATUS clear 2, FLUSH_TX 1, W_TX_PAYLOAD 33, a STATUS
    # poll or two of 2, STATUS clear 2, CONFIG 2), at most 50 at the core's
    # speed. The timeout path took 10 ms more per fragment.
    send_us = 50 * 8 * BIT_INSTRUCTIONS / mips(sim, "ecu")
    for frags in _cycles(payloads).values():
        gaps = [b.t_us - a.t_us for a, b in zip(frags, frags[1:])]
        assert all(PACE_US <= g < PACE_US + send_us for g in gaps), (gaps, send_us)


def test_a_snapshot_every_200_ms_in_five_fragments(run):
    _, _, payloads = run
    cycles = _cycles(payloads)
    seqs = sorted(cycles)
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    for seq, frags in cycles.items():
        if frags is cycles[seqs[-1]] and len(frags) < FRAGMENTS:
            continue   # the run ended mid-snapshot
        assert len(frags) == FRAGMENTS, seq
        for idx, p in enumerate(frags):
            assert len(p.data) == FRAGMENT_SIZE
            assert p.data[:8] == bytes([0xEC, 0x03, idx, FRAGMENTS]) + seq.to_bytes(2, "little") \
                + bytes([0x06, 0x00]), p.data.hex()
    # Fragment 0 on the task's 200 ms grid from the second cycle on: the
    # first's tick base is taken before the radio's bring-up
    # (telemetry_task.cpp:301-308), as for the dash frames (test_ecu_diag.py).
    firsts = [cycles[s][0] for s in seqs[1:]]
    assert_cadence(firsts, period_us=PERIOD_US, jitter_us=KERNEL_TICK_US, min_count=5)


def test_the_snapshot_carries_the_cycle_s_seq_and_tick(run):
    """Reassembled, the snapshot's seq is the fragments', its tick advances
    200 ms a cycle, and it matches the same cycle's dash frames: 0x510's seq
    and 0x51B's tick (sampled just before, in send_dashboard)."""
    sim, _, payloads = run
    cycles = {s: f for s, f in _cycles(payloads).items() if len(f) == FRAGMENTS}
    status = {int.from_bytes(f.data[6:8], "little"): f
              for f in sim.can("can_dash").frames(0x510)}
    ticks = sim.can("can_dash").frames(0x51B)
    snap_ticks = []
    for seq, frags in sorted(cycles.items()):
        wire = b"".join(p.data[8:] for p in frags)
        assert len(wire) == FRAGMENTS * SLICE and wire[SNAPSHOT_SIZE:] == bytes(18)
        snap = wire[:SNAPSHOT_SIZE]
        assert int.from_bytes(snap[4:6], "little") == seq
        assert snap[15] == 0 and snap[96:] == bytes(6)
        tick = int.from_bytes(snap[0:4], "little")
        snap_ticks.append(tick)
        assert seq in status and status[seq].t_us < frags[0].t_us
        dash = max((f for f in ticks if f.t_us < frags[0].t_us), key=lambda f: f.t_us)
        assert 0 <= tick - int.from_bytes(dash.data[4:8], "little") <= 1, (seq, tick)
    assert {b - a for a, b in zip(snap_ticks[1:], snap_ticks[2:])} == {PERIOD_US // 1000}
