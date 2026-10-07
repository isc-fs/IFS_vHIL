"""The scenario suite's images: every image a system's boards run, from the
options tests/conftest.py registers (--ecu-elf, --ams-elf,
--can-bootloader-elf, default $VHIL_<X>_ELF). A system with one missing is
skipped, as tests/sim does."""
from pathlib import Path

import pytest

from vhil.system import REPO, System


@pytest.fixture(scope="session")
def images(request):
    def get(system: str) -> dict[str, Path]:
        fw = {}
        for b in System(REPO / "systems" / f"{system}.yaml").boards.values():
            parts = [(b.name, b.firmware["id"])]
            if b.bootloader is not None:
                parts.append((f"{b.name}.bootloader", b.bootloader["id"]))
            for key, ident in parts:
                elf = request.config.getoption(f"--{ident}-elf", default=None)
                if not elf or not Path(elf).is_file():
                    env = f"VHIL_{ident.upper().replace('-', '_')}_ELF"
                    pytest.skip(f"no {ident} image (--{ident}-elf or {env})")
                fw[key] = Path(elf)
        return fw
    return get
