import argparse

import uvicorn

from vhil.server import create_app

p = argparse.ArgumentParser(prog="python -m vhil.server")
p.add_argument("--host", default="127.0.0.1")
p.add_argument("--port", type=int, default=8080)
a = p.parse_args()
uvicorn.run(create_app(), host=a.host, port=a.port)
