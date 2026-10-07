# Co-simulation port

How a **plant** (something outside the electronics: a driver, an inverter
and motor, the vehicle) exchanges signals with a system on the virtual
bench. This is the contract MingoCIL attaches through ([#14](https://github.com/isc-fs/IFS_vHIL/issues/14),
vision principle 6). It is engine-agnostic: the vHIL side is Renode today,
and the plant side can be Python (`vhil/plants.py`) or an external engine.

## Signals

A system declares its port: a step and named signals, each one of five kinds.

```yaml
port:
  step_ms: 10
  signals:
    apps1:      {analog: ecu.PF8}                    # plant -> system: volts on an analog input
    start:      {gpio_in: ecu.PB5}                   # plant -> system: level on a GPIO input
    rtds:       {gpio_out: ecu.PB4}                  # system -> plant: level of a GPIO output
    inv_cmd:    {can_rx: {bus: can_inv, id: 0x360}}  # system -> plant: frames sent on a bus
    inv_state:  {can_tx: {bus: can_inv, id: 0x461}}  # plant -> system: frames to put on a bus
```

| Kind | Direction | Value | Meaning |
|---|---|---|---|
| `analog` | plant → system | volts (float) | Held on the pin until changed. |
| `gpio_in` | plant → system | bool | Held on the input until changed. |
| `gpio_out` | system → plant | bool | The output's level at the step boundary. |
| `can_rx` | system → plant | frames | Every frame with that ID the system sent on the bus during the last step, with its virtual timestamp (µs) and data. |
| `can_tx` | plant → system | frames | Standard frames to put on the bus at the start of the next step, in order. On a bus with `arbitration: true` they are offered then and go out as the bus lets them ([`can-bus.md`](can-bus.md)). |

Endpoints are board connectors from the catalogue (`<board>.<pin>`, e.g.
`ecu.PF8`); which car signal a pin carries on its backplane (`ecu.PF8` is
APPS_1) is in [`backplanes/`](backplanes/). CAN signals name a bus of the
system. `python -m vhil.system validate` checks
that each endpoint is of the kind its signal says.

## Time

Virtual time advances in steps of `step_ms`, and only the vHIL advances it.
At each step boundary `t`:

1. Each plant reads what the system did during `[t − step, t)`: the frames
   it sent, and its outputs' levels at `t`.
2. Each plant writes what it drives: voltages and input levels take effect
   at `t`, and frames are sent at `t`.
3. The system runs `[t, t + step)`.

While plants compute, virtual time stands still, so a plant may take as long
as it needs. That lock-step makes a run deterministic: the same firmware,
system, plants and inputs give the same frames at the same virtual times
(`tests/sim/test_cosim.py::test_the_loop_is_deterministic`).

The step bounds a plant's reaction time. At 10 ms a plant answers a frame
within 10–20 ms of virtual time; choose the step from the fastest loop the
plant closes. Lock-step costs wall time, about 15 s per virtual second on a
desktop at 10 ms, mostly synchronisation, not emulation.

## In Python

```python
from vhil.sim import Sim
from vhil.cosim import Port
from vhil.plants import AcuStimulus, Inverter, Pedals

with Sim("systems/ecu.yaml", {"ecu": "ECU08.elf", "ecu.bootloader": "CAN_BL.elf"}) as sim:
    t0 = sim.wait_for_app()     # after the bootloader's 2 s auto-jump window
    port = Port(sim)
    inverter = Inverter()
    port.run([AcuStimulus(), Pedals(driver, start_us=t0), inverter], ms=5000)
```

A plant is any object with `step(port, t_us)`. Inside it, `port.received(name)`,
`port.level(name)`, `port.set_voltage(name, v)`, `port.set_level(name, b)` and
`port.send(name, data)` act on the signals by name.

## For an external engine (MingoCIL)

The exchange is the same, carried by a transport instead of a Python call:
at each boundary the vHIL sends the engine `t` and the system → plant values,
and waits for the plant → system values before stepping. That transport is
not built yet. When MingoCIL needs it, it should be a thin adapter that
presents the engine as one more plant, so the timing contract above stays as
it is.

## Scripted plants today

- **`Inverter`**: the traction inverter's application-state machine as the
  ECU firmware's bench notes describe it (Standby → Ready → TorqueEnable, the
  fault-reset sequences, Shutdown), its 0x461/0x466/0x463 frames, and a
  first-order motor speed. The motor model is a placeholder for the vehicle.
- **`Pedals`**: a scripted driver over the car's calibrated APPS and brake
  sensor spans and the START button.
- **`AcuStimulus`**: *not a plant*. It stands in for the AMS on `ecu.yaml`,
  which has none. `ecu-ams.yaml` has the real AMS; a tractive-system plant
  closing precharge through the AMS's contactor outputs is the faithful next
  step.

## Cell source

The AMS reads its cells and NTCs from the LTC6811 chain model
(`models/renode/IsoSpi.cs`). Today a native test sets them through the
monitor (`SetCell`, `SetAllCells`, `SetTemperature`, `SetAuxRaw` per chip;
`SetAllCells`, `StopReply` on the bridge), as `tests/sim/test_ams_*.py` do.
There is no Pico LTC emulator on the virtual bench and won't be (#150). When
the Simscape battery stacks arrive (M9), the stack plant drives the same
setters at each step boundary: one more plant, with the chain's chips (module
`m` = chips `2m` and `2m + 1`) as its signals. The model keeps no other
writer, so tests and the plant don't contend; a test that sets a cell is a
scripted stand-in for that plant.
