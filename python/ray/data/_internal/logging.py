import logging
import logging.config
import os
from typing import Optional, List

import yaml

import ray

DEFAULT_TEXT_FORMATTER = (
    "%(asctime)s\t%(levelname)s %(filename)s:%(lineno)s -- %(message)s"  # noqa: E501
)
DEFAULT_JSON_FORMATTER = ray._private.ray_logging.formatters.JSONFormatter
DEFAULT_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "ray": {"format": DEFAULT_TEXT_FORMATTER},
        "ray_json": {
            "class": f"{DEFAULT_JSON_FORMATTER.__module__}.{DEFAULT_JSON_FORMATTER.__name__}"
        },
    },
    "filters": {
        "console_filter": {"()": "ray.data._internal.logging.HiddenRecordFilter"},
        "core_context_filter": {
            "()": "ray._private.ray_logging.filters.CoreContextFilter"
        },
    },
    "handlers": {
        "file": {
            "class": "ray.data._internal.logging.SessionFileHandler",
            "formatter": "ray",
            "filename": "ray-data.log",
        },
        "file_json": {
            "class": "ray.data._internal.logging.SessionFileHandler",
            "formatter": "ray_json",
            "filename": "ray-data.log",
            "filters": ["core_context_filter"],
        },
        "console": {
            "class": "ray._private.log.PlainRayHandler",
            "formatter": "ray",
            "level": "INFO",
            "filters": ["console_filter"],
        },
    },
    "loggers": {
        "ray.data": {
            "level": "DEBUG",
            "handlers": ["file", "console"],
            "propagate": False,
        },
        "ray.air.util.tensor_extensions": {
            "level": "DEBUG",
            "handlers": ["file", "console"],
            "propagate": False,
        },
    },
}

# Dictionary of substitutions to be performed when using JSON mode. Handlers with names
# corresponding to keys will be replaced by those corresponding to values.
RAY_DATA_LOG_HANDLER_JSON_SUBSTITUTIONS = {"file": "file_json"}

# Env. variable to specify the encoding of the file logs when using the default config.
RAY_DATA_LOG_ENCODING_ENV_VAR_NAME = "RAY_DATA_LOG_ENCODING"

# Env. variable to specify the logging config path use defaults if not set
RAY_DATA_LOGGING_CONFIG_ENV_VAR_NAME = "RAY_DATA_LOGGING_CONFIG"

_DATASET_LOGGER_HANDLER = {}
_ACTIVE_DATASET = None

# To facilitate debugging, Ray Data writes debug logs to a file. However, if Ray Data
# logs every scheduler loop, logging might impact performance. So, we add a "TRACE"
# level where logs aren't written by default.
#
# Use the following code to log a message at the "TRACE" level:
# ```
# logger.log(logging.getLevelName("TRACE"), "Your message here.")
# ````
logging.addLevelName(logging.DEBUG - 1, "TRACE")


class HiddenRecordFilter:
    """Filters out log records with the "hide" attribute set to True.

    This filter allows you to override default logging behavior. For example, if errors
    are printed by default, and you don't want to print a specific error, you can set
    the "hide" attribute to avoid printing the message.

    .. testcode::

        import logging
        logger = logging.getLogger("ray.data.spam")

        # This warning won't be printed to the console.
        logger.warning("ham", extra={"hide": True})
    """

    def filter(self, record):
        return not getattr(record, "hide", False)


class SessionFileHandler(logging.Handler):
    """A handler that writes to a log file in the Ray session directory.

    The Ray session directory isn't available until Ray is initialized, so this handler
    lazily creates the file handler when you emit a log record.

    Args:
        filename: The name of the log file. The file is created in the 'logs' directory
            of the Ray session directory.
    """

    def __init__(self, filename: str):
        super().__init__()
        self._filename = filename
        self._handler = None
        self._formatter = None
        self._path = None

    def emit(self, record):
        if self._handler is None:
            self._try_create_handler()
        if self._handler is not None:
            self._handler.emit(record)

    def setFormatter(self, fmt: logging.Formatter) -> None:
        if self._handler is not None:
            self._handler.setFormatter(fmt)
        self._formatter = fmt

    def _try_create_handler(self):
        assert self._handler is None

        log_directory = get_log_directory()
        if log_directory is None:
            return

        os.makedirs(log_directory, exist_ok=True)

        self._path = os.path.join(log_directory, self._filename)
        self._handler = logging.FileHandler(self._path)
        if self._formatter is not None:
            self._handler.setFormatter(self._formatter)


def _get_logging_config() -> Optional[dict]:
    def _load_logging_config(config_path: str):
        with open(config_path) as file:
            config = yaml.safe_load(file)
        return config

    # Dynamically load env vars
    config_path = os.environ.get(RAY_DATA_LOGGING_CONFIG_ENV_VAR_NAME)
    log_encoding = os.environ.get(RAY_DATA_LOG_ENCODING_ENV_VAR_NAME)

    if config_path is not None:
        config = _load_logging_config(config_path)
    else:
        config = DEFAULT_CONFIG
        if log_encoding is not None and log_encoding.upper() == "JSON":
            for logger in config["loggers"].values():
                for (
                    old_handler_name,
                    new_handler_name,
                ) in RAY_DATA_LOG_HANDLER_JSON_SUBSTITUTIONS.items():
                    logger["handlers"].remove(old_handler_name)
                    logger["handlers"].append(new_handler_name)

    return config


def _get_logger_names() -> List[str]:
    logger_config = _get_logging_config().get("loggers", {})

    return list(logger_config.keys())


def configure_logging() -> None:
    """Configure the Python logger named 'ray.data'.

    This function loads the configration YAML specified by "RAY_DATA_LOGGING_CONFIG"
    environment variable. If the variable isn't set, this function loads the default
    "logging.yaml" file that is adjacent to this module.

    If "RAY_DATA_LOG_ENCODING" is specified as "JSON" we will enable JSON logging mode
    if using the default logging config.
    """

    # Dynamically load env vars
    config_path = os.environ.get(RAY_DATA_LOGGING_CONFIG_ENV_VAR_NAME)
    log_encoding = os.environ.get(RAY_DATA_LOG_ENCODING_ENV_VAR_NAME)
    config = _get_logging_config()

    logging.config.dictConfig(config)

    # After configuring logger, warn if RAY_DATA_LOGGING_CONFIG is used with
    # RAY_DATA_LOG_ENCODING, because they are not both supported together.
    if config_path is not None and log_encoding is not None:
        logger = logging.getLogger(__name__)
        logger.warning(
            "Using `RAY_DATA_LOG_ENCODING` is not supported with "
            + "`RAY_DATA_LOGGING_CONFIG`"
        )


def reset_logging() -> None:
    """Reset the logger named 'ray.data' to its initial state.

    Used for testing.
    """
    logger = logging.getLogger("ray.data")
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)


def get_log_directory() -> Optional[str]:
    """Return the directory where Ray Data writes log files.

    If Ray isn't initialized, this function returns ``None``.
    """
    global_node = ray._private.worker._global_node
    if global_node is None:
        return None

    session_dir = global_node.get_session_dir_path()
    return os.path.join(session_dir, "logs", "ray-data")


def _get_default_formatter() -> logging.Formatter:
    log_encoding = os.environ.get(RAY_DATA_LOG_ENCODING_ENV_VAR_NAME)
    if log_encoding is not None and log_encoding.upper() == "JSON":
        return DEFAULT_JSON_FORMATTER()

    return logging.Formatter(DEFAULT_TEXT_FORMATTER)


def _create_dataset_log_handler(dataset_id: str) -> SessionFileHandler:
    """Create a log handler for a dataset with the given ID.

    Args:
        dataset_id: The ID of the dataset.

    Returns:
        A log handler for the dataset.
    """
    handler = SessionFileHandler(filename=f"ray-data-{dataset_id}.log")
    handler.setFormatter(_get_default_formatter())

    return handler


def register_dataset_logger(dataset_id: str) -> None:
    """Create a log handler for a dataset with the given ID. Activate the handler if
    this is the only active dataset. Otherwise, print a warning to that handler and
    keep it inactive until it becomes the only active dataset.

    Args:
        dataset_id: The ID of the dataset.
    """
    global _DATASET_LOGGER_HANDLER
    global _ACTIVE_DATASET
    loggers = [logging.getLogger(name) for name in _get_logger_names()]
    log_handler = _create_dataset_log_handler(dataset_id)

    # The per-dataset log will always have the full context about its registration,
    # regardless of whether it is active or inactive.
    local_logger = logging.getLogger(__name__)
    local_logger.addHandler(log_handler)
    local_logger.info("Registered dataset logger for dataset %s", dataset_id)

    _DATASET_LOGGER_HANDLER[dataset_id] = log_handler
    if not _ACTIVE_DATASET:
        _ACTIVE_DATASET = dataset_id
        for logger in loggers:
            logger.addHandler(log_handler)
    else:
        local_logger.info(
            f"{dataset_id} registers for logging while another dataset "
            f"{_ACTIVE_DATASET} is also logging. For performance reasons, we will not "
            f"log to the dataset {dataset_id} until it is the only active dataset."
        )
    local_logger.removeHandler(log_handler)


def unregister_dataset_logger(dataset_id: str) -> None:
    """Remove the logger for a dataset with the given ID.

    Args:
        dataset_id: The ID of the dataset.
    """
    global _DATASET_LOGGER_HANDLER
    global _ACTIVE_DATASET
    loggers = [logging.getLogger(name) for name in _get_logger_names()]

    log_handler = _DATASET_LOGGER_HANDLER.pop(dataset_id, None)

    if _ACTIVE_DATASET == dataset_id:
        _ACTIVE_DATASET = None
        if _DATASET_LOGGER_HANDLER:
            # If there are still active dataset loggers, activate the first one.
            register_dataset_logger(next(iter(_DATASET_LOGGER_HANDLER.keys())))

    if log_handler:
        for logger in loggers:
            logger.removeHandler(log_handler)
        log_handler.close()
