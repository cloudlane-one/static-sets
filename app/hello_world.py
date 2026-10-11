# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.19.5
# ---

# %% [markdown]
# # Hello World Greeter
#
# This notebook showcases and tests the `Greeter` class from the `src.hello_world` module.

# %% [markdown]
# ## Imports

# %%
# Activate autoload extension to auto-reload external modules, when they change.
# %load_ext autoreload
# %autoreload 2

# Activate colorful and structured cell output via Rich.
# %load_ext rich

# %%
from based_pyproject.hello_world import Greeter

# %% [markdown]
# ## Class instantiation

# %%
greeter = Greeter(
    # This should ask the user for input via a prompt (look above in VSCode).
    name=input("Who should greet the world?"),
)

# %% [markdown]
# ## Greeting tests

# %%
greeter.say_hello()
