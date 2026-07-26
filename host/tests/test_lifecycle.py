from collections import OrderedDict

from osca_host.episode import Episode
from osca_host.lifecycle import EpisodeLifecycle
from osca_host.policy import PolicyInterceptor


def _episode(status="running"):
    return Episode("EP-1", "pkg", "AW-1", "AW-1/T1", "now", None, {}, {}, status=status)


def test_terminal_transition_releases_budget_and_suspension_index():
    episode = _episode("suspended_pending_approval")
    episodes = OrderedDict([(episode.episode_id, episode)])
    suspensions = {"CH-1": episode.episode_id}
    policy = PolicyInterceptor("pkg", {}, {})
    policy._tool_calls[episode.episode_id] = 1
    policy._tokens[episode.episode_id] = 80
    lifecycle = EpisodeLifecycle(episodes, suspensions)

    lifecycle.finish(episode, "completed", policy=policy)
    lifecycle.finish(episode, "completed", policy=policy)

    assert episode.status == "completed"
    assert suspensions == {}
    assert policy.episode_budget_used(episode.episode_id) == (0, 0)


def test_suspend_keeps_budget():
    episode = _episode()
    episodes = OrderedDict([(episode.episode_id, episode)])
    suspensions = {}
    policy = PolicyInterceptor("pkg", {}, {})
    policy._tool_calls[episode.episode_id] = 1
    lifecycle = EpisodeLifecycle(episodes, suspensions)

    lifecycle.suspend(episode, "CH-1")

    assert episode.status == "suspended_pending_approval"
    assert suspensions == {"CH-1": "EP-1"}
    assert policy.episode_budget_used("EP-1") == (1, 0)
