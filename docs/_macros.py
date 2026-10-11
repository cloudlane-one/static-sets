"""Mkdocs-macros helper module."""

import os

import dotenv

dotenv.load_dotenv()


def define_env(env):
    # add to the dictionary of variables available to markdown pages:
    env.variables["env_vars"] = dict(os.environ)
