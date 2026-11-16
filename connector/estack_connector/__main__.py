import logging
import os
import signal
import sys

from estack_connector.client import EstackClient
from estack_connector.config import ConfigError, load_config
from estack_connector.runner import VERSION, Connector

DEFAULT_CONFIG_PATH = "/config/databases.yaml"


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "mapepire_python"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        config = load_config(os.environ.get("CONNECTOR_CONFIG", DEFAULT_CONFIG_PATH))
    except ConfigError as exc:
        problem = str(exc)
    else:
        problem = None
    if problem is not None:
        logging.getLogger("estack_connector").error("Configuração inválida: %s", problem)
        return 2

    client = EstackClient(config.estack_url, config.token, VERSION)
    connector = Connector(config, client)
    signal.signal(signal.SIGTERM, lambda *_: connector.stop())
    try:
        connector.run()
    except KeyboardInterrupt:
        connector.stop()
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
