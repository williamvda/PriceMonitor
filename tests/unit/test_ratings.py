"""Unit tests for the batched retailer-ratings lookup."""

import json
import logging

import pytest
from llmbridge.exceptions import LLMConnectionError
from llmbridge.models import PromptResponse

import price_monitor.search.searcher as searcher_module
from price_monitor.app_config import LLMConfig
from price_monitor.search.ratings import RATING_BATCH, RatingsLookup


def _response(text: str) -> PromptResponse:
    return PromptResponse(
        text=text,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        model="gemini-3.7-flash",
        provider="gemini",
        latency_ms=1.0,
        raw_response={},
    )


class FakeClient:
    def __init__(self, *results):
        self.results = list(results)
        self.prompts: list[str] = []

    def prompt(self, prompt_text: str, **kwargs) -> PromptResponse:
        self.prompts.append(prompt_text)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def logger():
    return logging.getLogger("test")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Skip the retry backoff: three attempts would add six real seconds."""
    monkeypatch.setattr(searcher_module.time, "sleep", lambda _seconds: None)


def _lookup(logger, payload: str, report: str = "a report") -> RatingsLookup:
    return RatingsLookup(
        LLMConfig(api_key="k"),
        logger,
        search_client=FakeClient(_response(report)),
        format_client=FakeClient(_response(payload)),
    )


def _payload(entries: list[dict]) -> str:
    return json.dumps({"ratings": entries})


def test_a_rating_is_read_back_for_the_site_requested(logger):
    lookup = _lookup(
        logger,
        _payload(
            [
                {
                    "site": "argos.co.uk",
                    "rating": 4.1,
                    "reviews": 13305,
                    "source": "trustpilot",
                }
            ]
        ),
    )
    ratings = lookup.fetch(["argos.co.uk"])
    assert ratings["argos.co.uk"].rating == 4.1
    assert ratings["argos.co.uk"].reviews == 13305


def test_a_site_that_was_asked_about_but_not_returned_is_still_answered(logger):
    """Every requested site comes back, so its `checked` gets stamped and it
    is not looked up again tomorrow."""
    lookup = _lookup(logger, _payload([]))
    ratings = lookup.fetch(["tiny.co.uk"])
    assert set(ratings) == {"tiny.co.uk"}
    assert ratings["tiny.co.uk"].rating is None


def test_a_site_returned_but_never_asked_about_is_ignored(logger):
    lookup = _lookup(
        logger,
        _payload(
            [
                {
                    "site": "elsewhere.co.uk",
                    "rating": 4.0,
                    "reviews": 100,
                    "source": "google",
                }
            ]
        ),
    )
    assert set(lookup.fetch(["argos.co.uk"])) == {"argos.co.uk"}


def test_a_rating_outside_the_five_point_scale_is_stored_as_none(logger):
    """A rating out of 10, or a review count mistaken for a score, is not a
    trustworthy five-point value."""
    lookup = _lookup(
        logger,
        _payload(
            [
                {
                    "site": "argos.co.uk",
                    "rating": 45.0,
                    "reviews": 100,
                    "source": "google",
                }
            ]
        ),
    )
    assert lookup.fetch(["argos.co.uk"])["argos.co.uk"].rating is None


def test_a_boolean_rating_is_not_read_as_one(logger):
    lookup = _lookup(
        logger,
        _payload(
            [
                {
                    "site": "argos.co.uk",
                    "rating": True,
                    "reviews": 100,
                    "source": "google",
                }
            ]
        ),
    )
    assert lookup.fetch(["argos.co.uk"])["argos.co.uk"].rating is None


def test_matching_ignores_www_and_case(logger):
    lookup = _lookup(
        logger,
        _payload(
            [
                {
                    "site": "WWW.Argos.co.uk",
                    "rating": 4.1,
                    "reviews": 900,
                    "source": "trustpilot",
                }
            ]
        ),
    )
    assert lookup.fetch(["argos.co.uk"])["argos.co.uk"].rating == 4.1


def test_an_unparseable_reply_answers_every_site_with_nothing(logger):
    lookup = _lookup(logger, "not json at all")
    ratings = lookup.fetch(["argos.co.uk"])
    assert ratings == {}


def test_a_raising_client_never_escapes(logger):
    """A ratings failure must not cost a sweep its prices."""
    lookup = RatingsLookup(
        LLMConfig(api_key="k"),
        logger,
        search_client=FakeClient(
            LLMConnectionError("down"),
            LLMConnectionError("down"),
            LLMConnectionError("down"),
        ),
        format_client=FakeClient(_response("{}")),
    )
    assert lookup.fetch(["argos.co.uk"]) == {}


def test_no_sites_makes_no_calls(logger):
    search = FakeClient()
    lookup = RatingsLookup(
        LLMConfig(api_key="k"), logger, search_client=search, format_client=FakeClient()
    )
    assert lookup.fetch([]) == {}
    assert search.prompts == []


def test_the_prompt_names_every_site_and_forbids_estimating(logger):
    lookup = _lookup(logger, _payload([]))
    lookup.fetch(["argos.co.uk", "currys.co.uk"])
    prompt = lookup._search_client.prompts[0]
    assert "argos.co.uk" in prompt
    assert "currys.co.uk" in prompt
    assert "do not estimate one" in prompt


def test_the_batch_size_is_ten():
    """One grounded call per update, whatever state the cache is in."""
    assert RATING_BATCH == 10
