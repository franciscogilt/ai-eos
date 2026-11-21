from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import httpx
import openai
import pytest
from agents import Agent, OpenAIChatCompletionsModel, OpenAIResponsesModel, RunConfig, Runner
from pydantic import ValidationError
from tests.fakes import FakeModel, MemoryRecorder, response

from aieos.agents.independence import IndependenceError, check_model_independence
from aieos.agents.registry import load_all
from aieos.contracts.agent_contract import ModelRef
from aieos.providers.circuit import CircuitBreaker
from aieos.providers.config import Price, ProviderConfig, RoutingConfig
from aieos.providers.metering import BudgetExceededError, MeteringContext
from aieos.providers.models import Candidate, MeteredModel, ModelUnavailableError
from aieos.providers.pricing import TokenUsage, cost_usd
from aieos.providers.router import ModelNotConfiguredError, ModelRouter, openai_model_factory

PRICE = Price(input_per_mtok=Decimal("2"), cached_input_per_mtok=Decimal("0.5"), output_per_mtok=Decimal("8"))
CTX = MeteringContext(
    org_id=uuid.uuid4(),
    workflow_id=uuid.uuid4(),
    agent_run_id=uuid.uuid4(),
    agent_key="developer",
    run_max_cost_usd=Decimal("10"),
    run_max_tokens=10**6,
)


def transient() -> Exception:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://provider.test/v1/responses"))


def routing(**overrides) -> RoutingConfig:
    base = {
        "api_version": "aieos/v1",
        "providers": {
            "openai": {"kind": "openai", "api_key_env": "OPENAI_API_KEY"},
            "anthropic": {
                "kind": "openai_compatible",
                "api_key_env": "SECONDARY_PROVIDER_API_KEY",
                "base_url_env": "SECONDARY_PROVIDER_BASE_URL",
            },
        },
        "models": {
            "openai": {"reasoning_high": "model-a", "reasoning_standard": "model-s"},
            "anthropic": {"reasoning_high": "model-b"},
        },
        "pricing": {k: PRICE.model_dump() for k in ("openai/model-a", "openai/model-s", "anthropic/model-b")},
    }
    base.update(overrides)
    return RoutingConfig.model_validate(base)


@pytest.fixture(scope="module")
def contracts(definitions_dir, prompts_dir, catalog):
    return {k: v.contract for k, v in load_all(definitions_dir, prompts_dir=prompts_dir, catalog=catalog).items()}


# ------------------------------------------------------------------ pricing and breaker
def test_cost_with_cached_tokens():
    # 600 uncached * 2 + 400 cached * 0.5 + 200 out * 8 = 1200 + 200 + 1600 = 3000 per 1M -> $0.003
    assert cost_usd(TokenUsage(1000, 400, 200), PRICE) == Decimal("0.003000")


def test_cache_writes_priced_separately_when_configured():
    usage = TokenUsage(1000, 0, 0, cache_write_input_tokens=500)
    assert cost_usd(usage, PRICE) == Decimal("0.002000")  # unset rate: billed as input
    priced = PRICE.model_copy(update={"cache_write_input_per_mtok": Decimal("4")})
    assert cost_usd(usage, priced) == Decimal("0.003000")  # 500*2 + 500*4


def test_circuit_breaker_cycle():
    now = [0.0]
    b = CircuitBreaker(failure_threshold=2, reset_after_seconds=10, clock=lambda: now[0])
    b.record_failure()
    assert b.allow()
    b.record_failure()
    assert b.state == "open" and not b.allow()
    now[0] = 10
    assert b.state == "half_open" and b.allow() and not b.allow()  # exactly one trial
    b.record_failure()
    assert b.state == "open"
    now[0] = 20
    assert b.allow()
    b.record_success()
    assert b.state == "closed"


# ------------------------------------------------------------------ config and routing
@pytest.mark.parametrize(
    "override",
    [
        {"models": {"nope": {"fast": "m"}}},
        {"pricing": {"openai": PRICE.model_dump()}},
        {"pricing": {"ghost/model": PRICE.model_dump()}},
        {"providers": {"x": {"kind": "openai_compatible", "api_key_env": "X_KEY"}}},
    ],
)
def test_routing_config_rejects(override):
    with pytest.raises(ValidationError):
        routing(**override)


def test_shipped_routing_file_is_valid_but_unconfigured(contracts):
    from pathlib import Path

    from aieos.providers.config import load_routing_config

    cfg = load_routing_config(Path(__file__).resolve().parents[2] / "engineering" / "model-routing.yaml")
    with pytest.raises(ModelNotConfiguredError, match="placeholder"):
        ModelRouter(cfg).resolve(contracts["developer"].model)


def test_resolve_keeps_contract_provider(contracts):
    router = ModelRouter(routing())
    assert router.resolve(contracts["developer"].model) == [ModelRef(provider="openai", model="model-a")]
    assert router.resolve(contracts["qa_security"].model) == [ModelRef(provider="anthropic", model="model-b")]


def test_unpriced_model_refused(contracts):
    router = ModelRouter(routing(pricing={"anthropic/model-b": PRICE.model_dump()}))
    with pytest.raises(ModelNotConfiguredError, match="no price"):
        router.resolve(contracts["developer"].model)


def test_independence_holds_and_detects_violation(contracts):
    router = ModelRouter(routing())
    check_model_independence(contracts, router)
    qa = contracts["qa_security"]
    bad_qa = qa.model_copy(
        update={
            "model": qa.model.model_copy(
                update={"primary": ModelRef(provider="openai", model="REPLACE_WITH_ROUTING_DEFAULT")}
            )
        }
    )
    with pytest.raises(IndependenceError, match="must not use the same model"):
        check_model_independence({**contracts, "qa_security": bad_qa}, router)


def test_factory_requires_env_and_builds_right_class(monkeypatch):
    cfg = routing()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ModelNotConfiguredError, match="OPENAI_API_KEY"):
        openai_model_factory("openai", cfg.providers["openai"], "model-a")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    monkeypatch.setenv("SECONDARY_PROVIDER_API_KEY", "test-not-a-key")
    monkeypatch.setenv("SECONDARY_PROVIDER_BASE_URL", "https://compat.example/v1")
    assert isinstance(openai_model_factory("openai", cfg.providers["openai"], "model-a"), OpenAIResponsesModel)
    compat = openai_model_factory("anthropic", cfg.providers["anthropic"], "model-b")
    assert isinstance(compat, OpenAIChatCompletionsModel)


# ------------------------------------------------------------------ metered model
def metered(*models: FakeModel, recorder=None, breakers=None):
    cands = [
        Candidate(ModelRef(provider=f"p{i}", model=f"m{i}"), m, PRICE, (breakers or {}).get(i, CircuitBreaker()))
        for i, m in enumerate(models)
    ]
    return MeteredModel(cands, recorder=recorder or MemoryRecorder(), ctx=CTX)


def call(model: MeteredModel):
    return asyncio.run(model.get_response(None, "hi", None, [], None, [], None))


def test_records_usage_and_cost():
    rec = MemoryRecorder()
    call(metered(FakeModel(response(inp=1000, cached=400, out=200, rid="abc")), recorder=rec))
    assert rec.records == [
        {
            "provider": "p0",
            "model": "m0",
            "usage": TokenUsage(1000, 400, 200, 0),
            "cost": Decimal("0.003000"),
            "request_id": "abc",
        }
    ]


def test_falls_back_on_transient_error():
    primary, fallback = FakeModel(transient()), FakeModel(response(rid="fb"))
    rec = MemoryRecorder()
    call(metered(primary, fallback, recorder=rec))
    assert primary.calls == 1 and fallback.calls == 1 and rec.records[0]["provider"] == "p1"


def test_non_transient_error_is_not_masked():
    fallback = FakeModel()
    with pytest.raises(ValueError):
        call(metered(FakeModel(ValueError("bad request")), fallback))
    assert fallback.calls == 0


def test_open_circuit_skips_provider_and_all_down_raises():
    breaker = CircuitBreaker(failure_threshold=1)
    primary = FakeModel(transient(), transient())
    fallback = FakeModel(transient(), transient())
    model = metered(primary, fallback, breakers={0: breaker})
    with pytest.raises(ModelUnavailableError):
        call(model)
    assert breaker.state == "open"
    with pytest.raises(ModelUnavailableError):
        call(model)
    assert primary.calls == 1 and fallback.calls == 2  # primary skipped on the second call


def test_budget_checked_before_calling_provider():
    fake = FakeModel()
    with pytest.raises(BudgetExceededError):
        call(metered(fake, recorder=MemoryRecorder(budget_usd=Decimal("0"))))
    assert fake.calls == 0


def test_streaming_disabled():
    with pytest.raises(NotImplementedError):
        metered(FakeModel()).stream_response()


# ------------------------------------------------------------------ through the Agents SDK runner
def test_agents_sdk_run_is_metered(contracts):
    fake = FakeModel(response("Final answer", rid="run-1"))
    router = ModelRouter(routing(), model_factory=lambda provider, cfg, name: fake)
    rec = MemoryRecorder()
    provider = router.provider_for_run(contracts["developer"], CTX, rec)
    agent = Agent(name="developer", instructions="Be brief.", model=contracts["developer"].model.profile)
    result = asyncio.run(
        Runner.run(agent, "hello", run_config=RunConfig(model_provider=provider, tracing_disabled=True))
    )
    assert result.final_output == "Final answer"
    assert [r["request_id"] for r in rec.records] == ["run-1"]


def test_run_provider_refuses_other_model_names(contracts):
    router = ModelRouter(routing(), model_factory=lambda provider, cfg, name: FakeModel())
    provider = router.provider_for_run(contracts["developer"], CTX, MemoryRecorder())
    with pytest.raises(ModelNotConfiguredError, match="pinned"):
        provider.get_model("gpt-something-else")


def test_provider_config_defaults():
    assert ProviderConfig(kind="openai", api_key_env="K").timeout_seconds == 120.0
