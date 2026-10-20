"""The FMI master (vhil/fmi.py) on ecu-ams: the reference tractive-system
FMU (tests/fmu/ts_reference.c), compiled here, in place of the Python
stand-in, against the real AMS and ECU. The car arms on the FMU's link as
it does on the stand-in's, and a run is the same every time. When IFSSIM's
electrical FMU lands (isc-fs/IFSSIM#669) it runs here the same way."""
import shutil

import pytest

from test_plant_ts import ERROR, RUN, _arm
from vhil.fmi import Fmu, FmuPlant, build_reference_fmu

pytestmark = pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler for the reference FMU")

# Impact selection (ci/impact.yaml, docs/ci.md): boards, systems, subsystems.
pytestmark = [pytestmark, pytest.mark.ecu, pytest.mark.ams, pytest.mark.system_ecu_ams,
              pytest.mark.plant]


@pytest.fixture(scope="module")
def fmu_path(tmp_path_factory):
    return build_reference_fmu(tmp_path_factory.mktemp("fmu") / "ts.fmu")


@pytest.fixture(scope="module")
def on_fmu(images, fmu_path):
    return _arm(images, lambda: FmuPlant(Fmu(fmu_path), cells="isospi"))


def test_the_car_arms_on_the_fmu_s_dc_link(on_fmu):
    assert ERROR not in on_fmu["states"], on_fmu["states"]
    assert on_fmu["states"][-1] == RUN, on_fmu["states"]
    plant = on_fmu["plant"]
    assert plant.inputs["v_pack"] == pytest.approx(351.5)
    rising = sorted({v for _, v, flags in on_fmu["heartbeat"] if flags & 0x2 and 0 < v < 330})
    assert len(rising) > 20, f"relayed link values during precharge: {rising}"


def test_the_fmu_and_the_stand_in_arm_the_car_alike(images, on_fmu):
    """The same AMS states, sampled every 100 ms, on the FMU as on the
    stand-in: the FMU applies an input at its next 5 ms grid point, which
    moves the curve by less than a sample."""
    stand_in = _arm(images)
    assert on_fmu["states"] == stand_in["states"]


def test_two_fmu_runs_are_the_same(images, fmu_path, on_fmu):
    again = _arm(images, lambda: FmuPlant(Fmu(fmu_path), cells="isospi"))
    for key in ("heartbeat", "vdc", "acu", "states"):
        assert again[key] == on_fmu[key], f"{key} differs between two runs"
