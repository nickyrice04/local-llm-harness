import os


def read_env(name, default=None):
    return os.environ.get(name, default)
