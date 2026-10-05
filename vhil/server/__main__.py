import argparse
import logging

import uvicorn

from vhil.server import create_app
from vhil.server.config import Settings, check_dev_bind

p = argparse.ArgumentParser(prog="python -m vhil.server")
p.add_argument("--host", default="127.0.0.1")
p.add_argument("--port", type=int, default=8080)
a = p.parse_args()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
settings = Settings.from_env()
check_dev_bind(settings, a.host)   # dev mode (no login) only on loopback, or explicitly allowed
uvicorn.run(create_app(settings), host=a.host, port=a.port)
