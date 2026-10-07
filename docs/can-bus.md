# CAN buses in virtual time

Every CAN bus of a system is the vHIL's bus model,
[`models/renode/VhilCanBus.cs`](../models/renode/VhilCanBus.cs) (#174):
frames wait for the bus and lose arbitration, they take their bit time, a
node nobody acknowledges goes error-passive, and the bus timeline and load
are exact in virtual time. A firmware sees what it would see on the car's
bus.

A bus can opt out with `arbitration: false`, and is then Renode's CAN hub:

```yaml
buses:
  can_acu: {kind: can, nodes: [ecu.FDCAN2, ams.FDCAN1], host_netdev: can2, arbitration: false}
```

The hub hands each frame to every other node the instant it is sent. It has
no bit time, no contention, no capacity and no ACK, and a frame from another
board lands at the next sync point (every `time.quantum_s`, 500 µs), so its
timestamps sit on the sync grid. Keep it for what truly needs it:
renode-test's CAN Tester keywords attach only to a hub (Renode's
`CANKeywords.cs`, `TestersProvider<CANTester, CANHub>`), so the smoke suites
render their script with `python -m vhil.system render --can-hub`, which
puts the hub on every bus. A test can opt out for one run with
`Sim(..., hub=["can_acu"])`. Test bus load, priority, ACK and frame timing
on the bus model, never on the hub.

The bus model was opt-in at first (`arbitration: true`, which now says what
the default says). Making it the default (#182) changed what some tests saw:
a frame is stamped at its end on the bus, not at a sync point, so periods
carry the firmware's own jitter. Such tests assert
a cadence (`vhil.sim.assert_cadence`: a fixed grid, no drift, no missing
frame, jitter under one kernel tick), not exact 10 000 µs intervals.

## What the bus does

The model works at frame granularity, with no bit-level physics. It follows
ISO 11898-1 (Bosch CAN 2.0B) for the bus, and RM0468's FDCAN chapter for the
controller (`Stm32H7Fdcan.cs`).

| | |
|---|---|
| Offers | Each FDCAN offers the request its Tx handler would send next: dedicated buffers and the Tx queue by lowest ID, the Tx FIFO in order. Each offer is stamped with the virtual time `TXBAR` was written. The probe and the SocketCAN bridge offer what they send, in order. |
| Arbitration | When the bus goes idle, every node with an offer starts. The arbitration field decides, dominant bit first: base ID, RTR/SRR, IDE, the ID extension, RTR. The losers wait for the next idle bus. A controller with `CCCR.DAR` set (`AutoRetransmission = DISABLE`) cancels a frame that loses instead (`TXBCF`). |
| Frame time | SOF to EOF at the transmitter's bit rate, from NBTP on the 24 MHz kernel clock. It counts the exact stuff bits for the frame's contents, then 3 bits of intermission. A receiver gets the frame at the next-to-last EOF bit; the transmitter completes at the last (`TXBTO`, `IR.TC`, the FIFO slot freed). |
| ACK | A frame no other node acknowledges gets an ACK error: error flag, delimiter and intermission, then a retry (or with DAR, a cancel). A node acknowledges when it is running: not in INIT, bus-off or bus monitoring mode, and at the same bit rate. The probe acknowledges unless `set_ack(False)` makes it listen only. |
| Errors | ACK errors only. The transmitter's TEC rises by 8 per error, but not once it is error-passive (rule 3, exception 1), and falls by 1 per good frame. An error-passive transmitter waits 8 extra bits after each frame. `ECR.TEC`, `PSR.LEC/EW/EP` read the result. A lone node climbs to 128, error-passive, and stays there, retrying. |
| Load | Busy time (frames, error frames, intermission) over a window: `CanBus.load()`. The worker writes it per bus and slice to the run's trace as `bus_load`, and the editor's Bus tab shows it. |

**Not modelled:**
- Bit, stuff, CRC and form errors.
- REC.
- Bus-off reached from errors. `ForceBusOff` still forces it.
- CAN FD frames, which are timed as classic frames.
- The FDCAN's Tx and error interrupts (`IR.TC/TCF/TFE`, the Tx event FIFO, `IR.EP/EW/BO/PEA`), and the Tx event FIFO itself.

The model logs these once when a firmware asks for them. Neither firmware does: both poll `PSR`, `TXFQS` and `TXBRP`.

## Virtual time and the sync quantum

Each board is its own Renode machine. The machines run in parallel and meet
at a sync point every quantum. An offer from one board is therefore known to
the others only at the next sync point, so the bus decides a frame only once
every offer that could compete with it is known:

- **One board on the bus** (and the probe, which only sends at sync points):
  the bus decides as the board offers, and every effect is exact.
- **Several boards:** the bus decides at each sync point, for the frames that
  start before it.

The bus timeline (`CanBus.timeline()`: offer, start, end and idle again,
outcome) is exact in virtual time either way. Each effect in a machine (RX,
TX complete, error state) is scheduled at the frame's virtual time while that
is still ahead. Otherwise it happens at the sync point, at most one quantum
late and never early. `CanBus.stats()["late"]` counts those effects. On
`ecu-ams` at 500 µs, about 60 % of cross-board effects are late, by up to
about 390 µs.

A quantum under the shortest frame (44 bit times, 88 µs at 500 kbit/s) would
make them all exact. With two boards it costs 3x to 5x in wall time, and it
slows the ECU's control loop, so the model doesn't do it. The spike numbers
are on [#174](https://github.com/isc-fs/IFS_vHIL/issues/174).

## Using it from a test

```python
with Sim("systems/ecu-ams.yaml", fw) as sim:
    can = sim.can("can_acu")
    logger = can.node("logger")        # a second probe: its own queue, ACK, TEC
    can.set_ack(False)                 # the main probe listens only
    ...
    for f in can.timeline(since_us=t0):   # BusFrame: start_ns, end_ns, free_ns, offer_ns,
        ...                               # node, id, outcome ("ok", "ack", "lost"), data
    can.load(t0, t1); can.stats(); can.nodes()   # nodes(): per node TEC, sent, ack errors, lost
```

On an arbitrated bus, `frames()` stamps each frame at the receiver's
next-to-last EOF bit, and `sent()` at the probe's own frame's last EOF bit.
A frame waiting for the bus is in neither. `vhil/canframe.py` computes a
frame's bits in Python, the same way as the model.
[`tests/sim/test_can_bus.py`](../tests/sim/test_can_bus.py) has one test per
exit criterion of #174, including a LOGFS-style flood on the ACU bus against
the ECU's heartbeat and the AMS's VcuStale.
