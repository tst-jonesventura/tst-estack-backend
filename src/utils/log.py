import logging


class EndpointFilter(logging.Filter):
    def __init__(self, endpoint: str):
        super().__init__()
        self.endpoint = endpoint

    def filter(self, record: logging.LogRecord) -> bool:
        return self.endpoint not in record.getMessage()


def setup_custom_logger(name):
    formatter = logging.Formatter(
        fmt="%(asctime)s - %(levelname)s - %(module)s - %(message)s"
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return logger
