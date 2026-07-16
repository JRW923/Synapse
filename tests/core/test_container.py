"""Tests for IoC container."""
import pytest
from typing import Protocol
from synapse.core.container import Container


class Greeter(Protocol):
    def greet(self) -> str: ...


class EnglishGreeter:
    def greet(self) -> str:
        return "hello"


class SpanishGreeter:
    def greet(self) -> str:
        return "hola"


def test_register_and_resolve_singleton():
    c = Container()
    g = EnglishGreeter()
    c.register(Greeter, g)
    resolved = c.resolve(Greeter)
    assert resolved is g
    assert resolved.greet() == "hello"


def test_register_factory():
    c = Container()
    call_count = [0]

    def factory():
        call_count[0] += 1
        return EnglishGreeter()

    c.register_factory(Greeter, factory)
    r1 = c.resolve(Greeter)
    r2 = c.resolve(Greeter)
    assert r1 is not r2  # New instance each time
    assert call_count[0] == 2


def test_resolve_unregistered_raises():
    c = Container()
    with pytest.raises(KeyError):
        c.resolve(Greeter)


def test_override():
    c = Container()
    c.register(Greeter, EnglishGreeter())
    assert c.resolve(Greeter).greet() == "hello"

    c.register(Greeter, SpanishGreeter())
    assert c.resolve(Greeter).greet() == "hola"  # Last registration wins


def test_resolve_with_generic_alias():
    """Protocol[X] and Protocol should resolve to the same registration."""
    c = Container()
    g = EnglishGreeter()
    c.register(Greeter, g)
    assert c.resolve(Greeter) is g
