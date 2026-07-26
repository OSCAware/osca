"""Episode 状态迁移与临时资源释放的单一入口。"""

from __future__ import annotations

from collections.abc import MutableMapping
from datetime import datetime

from osca_host.episode import Episode

TERMINAL_STATUSES = frozenset({"completed", "failed", "stopped"})


def finish_episode_state(episode: Episode, status: str, reason: str | None = None) -> Episode:
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"非法 Episode 终态：{status}")
    episode.status = status
    episode.stop_reason = reason
    episode.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return episode


class EpisodeLifecycle:
    def __init__(self, episodes: MutableMapping[str, Episode], suspensions: MutableMapping[str, str]):
        self._episodes = episodes
        self._suspensions = suspensions

    def suspend(self, episode: Episode, challenge_id: str) -> Episode:
        episode.status = "suspended_pending_approval"
        self._suspensions[challenge_id] = episode.episode_id
        return episode

    def finish(self, episode: Episode, status: str | None = None, reason: str | None = None, *, policy=None) -> Episode:
        if status is not None:
            finish_episode_state(episode, status, reason)
        elif episode.status not in TERMINAL_STATUSES:
            raise ValueError(f"Episode 尚非终态：{episode.status}")
        for challenge_id, episode_id in list(self._suspensions.items()):
            if episode_id == episode.episode_id:
                self._suspensions.pop(challenge_id, None)
        if policy is not None:
            policy.release_episode(episode.episode_id)
        return episode

    def evict(self, episode_id: str, *, policy=None) -> Episode | None:
        episode = self._episodes.pop(episode_id, None)
        if episode is not None:
            self.finish(episode, policy=policy)
        return episode
