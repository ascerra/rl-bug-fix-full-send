"""Tests for LLM provider abstraction."""

import pytest

from engine.integrations.llm import MockProvider, create_provider


@pytest.mark.asyncio
async def test_mock_provider_returns_canned_response():
    provider = MockProvider(responses=["Hello from mock"])
    response = await provider.complete(
        system_prompt="You are a test agent.",
        messages=[{"role": "user", "content": "Hi"}],
    )
    assert response.content == "Hello from mock"
    assert response.provider == "mock"
    assert response.model == "mock-model"


@pytest.mark.asyncio
async def test_mock_provider_cycles_responses():
    provider = MockProvider(responses=["first", "second"])
    r1 = await provider.complete("sys", [{"role": "user", "content": "a"}])
    r2 = await provider.complete("sys", [{"role": "user", "content": "b"}])
    r3 = await provider.complete("sys", [{"role": "user", "content": "c"}])
    assert r1.content == "first"
    assert r2.content == "second"
    assert r3.content == "first"  # cycles back


@pytest.mark.asyncio
async def test_mock_provider_records_calls():
    provider = MockProvider()
    await provider.complete(
        system_prompt="test prompt",
        messages=[{"role": "user", "content": "test message"}],
        temperature=0.5,
    )
    assert len(provider.call_log) == 1
    assert provider.call_log[0]["system_prompt"] == "test prompt"
    assert provider.call_log[0]["temperature"] == 0.5


def test_create_provider_mock():
    provider = create_provider("mock")
    assert provider.name == "mock"


def test_create_provider_unknown_raises():
    with pytest.raises(ValueError, match="Unknown provider"):
        create_provider("nonexistent")
