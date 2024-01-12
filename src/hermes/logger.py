# SPDX-FileCopyrightText: 2022 German Aerospace Center (DLR)
#
# SPDX-License-Identifier: Apache-2.0

# SPDX-FileContributor: Michael Meinel

import logging
import pathlib

import hermes.settings
from hermes.model.context import HermesContext

config = None
# This is the default logging configuration, required to see log output at all.
#  - Maybe it could possibly somehow be a somewhat good idea to move this into an own module ... later perhaps
_logging_config = {
    'version': 1,

    'formatters': {
        'plain': {'format': "%(message)s"},
        'logfile': {'format': "%(created)16f:%(name)20s:%(levelname)10s | %(message)s"},
        'auditlog': {'format': "%(asctime)s %(name)-20s  %(message)s"},
    },

    'handlers': {
        'terminal': {
            'class': "logging.StreamHandler",
            'formatter': "plain",
            'level': "INFO",
            'stream': "ext://sys.stdout",
        },

        'logfile': {
            'class': "logging.FileHandler",
            'formatter': "logfile",
            'level': "DEBUG",
            'filename': "./.hermes/hermes.log",
        },

        'auditfile': {
            'class': "logging.FileHandler",
            'formatter': "plain",
            'level': "DEBUG",
            'filename': "./.hermes/audit.log",
            'mode': "w",
        },
    },

    'loggers': {
        'cli': {'level': "DEBUG", 'handlers': ['terminal']},
        'hermes': {'level': "DEBUG", 'handlers': ['terminal', 'logfile']},
        'audit': {'level': "DEBUG", 'handlers': ['terminal', 'logfile']},
    },
}

# This dict caches all the different configuration sections already loaded
_config = {
    # We need some basic logging configuration to get logging up and running at all
    'logging': _logging_config,
}


def configure(settings: hermes.settings.HermesSettings, working_path: pathlib.Path):
    """
    Load the configuration from the given path as global hermes configuration.

    :param config_path: The path to a TOML file containing HERMES configuration.
    """
    if 'hermes' in _config and _config['hermes']:
        return

    # Load sane default paths for log files before potentially overwritting via configuration
    _config['logging']['handlers']['logfile']['filename'] = \
        working_path / HermesContext.hermes_cache_name / "hermes.log"
    _config['logging']['handlers']['auditfile']['filename'] = \
        working_path / HermesContext.hermes_cache_name / "audit.log"

    _config['hermes'] = settings
    global config
    config = settings
    print(config)
    _config['logging'] = settings.logging if settings.logging != {} else _config['logging']

# Might be a good idea to move somewhere else (see comment for _logging_config)?


_loggers = {}


def init_logging():
    if _loggers:
        return

    # Make sure the directories to hold the log files exists (or else create)
    pathlib.Path(_config['logging']['handlers']['logfile']['filename']).parent.mkdir(exist_ok=True, parents=True)
    pathlib.Path(_config['logging']['handlers']['auditfile']['filename']).parent.mkdir(exist_ok=True, parents=True)

    # Inintialize logging system
    import logging.config

    logging.config.dictConfig(_config['logging'])
    for log_name in _config['logging']['loggers']:
        _loggers[log_name] = logging.getLogger(log_name)


def getLogger(log_name):
    init_logging()
    if log_name not in _loggers:
        _loggers[log_name] = logging.getLogger(log_name)
    return _loggers.get(log_name)
