"""Test the `based_pyproject` module directly."""

from based_pyproject.hello_world import Greeter


def test_greeter_default():
    """Make sure the default Greeter behavior is as expected."""
    g = Greeter(name="Tester")

    greeting = g.say_hello()

    assert "Hello" in greeting
    assert "time" in greeting


def test_greeter_no_time():
    """Make sure Greeter time-telling can be turned off."""
    g = Greeter(name="Tester", tell_time=False)

    greeting = g.say_hello()

    assert "Hello" in greeting
    assert "time" not in greeting
