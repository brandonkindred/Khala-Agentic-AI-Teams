"""GitHub-backed repository reader for the PR code-review path.

The SE false-positive verifier can be handed a repository reader so it can
confirm that a file/module a finding calls "missing" already exists outside the
diff. For a pull request there is no local checkout, so this reader answers those
reads over the GitHub REST API against the PR's head commit.

It is duck-typed against SE's ``code_review_agent.repo_reader.RepoReader``
Protocol (``list_files`` / ``read_file``) and passed across the engine boundary
as plain data, so coding_team imports nothing from software_engineering_team.

Contract (matching the SE Protocol):
    - **Read-only, thread-safe.** Verification fans out across threads; the tree
      listing and per-file reads are memoized under a lock.
    - **Fail-safe.** ``read_file`` returns ``None`` (never raises) for a missing
      or unreadable path; ``list_files`` returns ``[]`` on failure. A reader
      failure only ever *keeps* a finding, never drops a real one.
    - **Bounded.** The tree is fetched once and its listing truncated to a cap;
      per-file reads are lazy and capped, so a review cannot fan out into
      unbounded API calls or an unbounded listing.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from .client import GitHubAPIError, GitHubClient

logger = logging.getLogger(__name__)

# Cap on the number of distinct existing files a single review will fetch on
# demand, so a review whose findings cite many off-diff paths cannot balloon
# into unbounded GitHub content requests. Once the cap is hit, further unseen
# paths read as ``None`` (treated as "cannot confirm" → the finding is kept).
DEFAULT_MAX_FETCHES = 200

# Cap on how many paths ``list_files`` returns, so the verifier's ``list_files``
# tool result cannot balloon into thousands of lines (token cost / context
# overflow) on a large repository. Mirrors DiskRepoReader's listing cap; files
# beyond the cap remain readable by exact path via ``read_file``.
DEFAULT_MAX_LISTED_FILES = 5_000


class GitHubRepoReader:
    """A repository reader answering reads over the GitHub API at ``head_sha``.

    Invariants:
        - Never mutates the repository; every method is a read.
        - The tree listing is fetched at most once, and each distinct file is
          fetched at most once, even under concurrency: both ``list_files`` and
          ``read_file`` use a single-flight pattern (an in-flight guard makes
          concurrent callers wait for the leader rather than each firing a GET),
          so neither ever double-fetches or double-counts the cap.
        - Total on-demand file fetches are capped at ``max_fetches``; the listing
          is capped at ``max_listed``.
        - The read cache holds at most ``max_fetches`` entries: every stored entry
          required a fetch, and fetches are capped, so it is size-bounded without
          a separate eviction knob (unlike ``DiskRepoReader``, whose disk reads
          are uncapped and therefore need an explicit ``max_read_cache``). It is a
          plain dict, not an LRU: nothing is ever evicted from it, so there is no
          recency order to track.
    """

    def __init__(
        self,
        client: GitHubClient,
        owner: str,
        repo: str,
        head_sha: str,
        *,
        max_fetches: int = DEFAULT_MAX_FETCHES,
        max_listed: int = DEFAULT_MAX_LISTED_FILES,
    ) -> None:
        """Bind the reader to one PR's head commit.

        Preconditions:
            - ``client`` is an open ``GitHubClient``; ``owner``/``repo`` are the
              repository coordinates; ``head_sha`` is the PR head commit SHA;
              ``max_fetches`` > 0 and ``max_listed`` > 0.
        """
        assert head_sha, "GitHubRepoReader requires a head commit SHA"
        assert max_fetches > 0 and max_listed > 0, "caps must be positive"
        self._client = client
        self._owner = owner
        self._repo = repo
        self._ref = head_sha
        self._max_fetches = max_fetches
        self._max_listed = max_listed
        # A single Condition guards all shared state and lets same-key readers
        # wait for the in-flight leader (single-flight), so a path is fetched
        # once even when the verifier fans out via shared.concurrency.parallel_map's
        # worker pool.
        self._cond = threading.Condition(threading.Lock())
        self._tree: Optional[List[str]] = None
        self._tree_inflight = False
        self._read_cache: Dict[str, Optional[str]] = {}
        self._inflight: set[str] = set()
        self._fetches = 0

    def list_files(self) -> List[str]:
        """Return repository-relative file paths at the head commit (cached).

        Postconditions:
            - Returns the head tree's blob paths, truncated to ``max_listed``
              (and possibly partial when GitHub truncates a very large tree),
              fetched once and memoized. Concurrent callers before the first
              fetch completes wait for it (single-flight) rather than each
              issuing their own tree GET. Returns ``[]`` on any API error
              (fail-safe). Never raises.
        """
        with self._cond:
            while True:
                if self._tree is not None:
                    return list(self._tree)
                if self._tree_inflight:
                    # Another thread is already fetching the tree; wait for it
                    # rather than issuing a duplicate GET.
                    self._cond.wait()
                    continue
                self._tree_inflight = True
                break
        try:
            tree = self._client.get_repository_tree(self._owner, self._repo, self._ref)
        except GitHubAPIError as exc:
            logger.debug(
                "GitHubRepoReader: tree fetch failed for %s@%s: %s", self._repo, self._ref, exc
            )
            tree = []
        tree = tree[: self._max_listed]
        with self._cond:
            self._tree = tree
            self._tree_inflight = False
            self._cond.notify_all()
            return list(self._tree)

    def read_file(self, path: str) -> Optional[str]:
        """Return the text of ``path`` at the head commit, or ``None``.

        Postconditions:
            - Returns the file's text for a readable file within the fetch cap;
              ``None`` for a blank path, a missing file, an API error, or once the
              per-review fetch cap is reached. Each distinct path is fetched at
              most once even under concurrent same-path reads (single-flight):
              a second reader waits for the leader and reuses its cached result,
              so the fetch cap is charged once per path. Never raises.
        """
        key = (path or "").strip()
        if not key:
            return None
        with self._cond:
            while True:
                if key in self._read_cache:
                    return self._read_cache[key]
                if key in self._inflight:
                    # Another thread is fetching this exact path; wait for it to
                    # populate the cache rather than issuing a duplicate GET.
                    self._cond.wait()
                    continue
                if self._fetches >= self._max_fetches:
                    logger.debug(
                        "GitHubRepoReader: fetch cap %s reached; not reading %s",
                        self._max_fetches,
                        key,
                    )
                    return None
                self._fetches += 1
                self._inflight.add(key)
                break
        content: Optional[str] = None
        try:
            content = self._fetch(key)
        finally:
            with self._cond:
                self._read_cache[key] = content
                self._inflight.discard(key)
                self._cond.notify_all()
        return content

    def _fetch(self, path: str) -> Optional[str]:
        """Fetch one file's content, degrading to ``None`` on any API error.

        Postconditions:
            - Returns the file's text, or ``None`` for a missing/unreadable path
              or an API error. Never raises.
        """
        try:
            return self._client.get_file_contents(self._owner, self._repo, path, self._ref)
        except GitHubAPIError as exc:
            logger.debug("GitHubRepoReader: read failed for %s@%s: %s", path, self._ref, exc)
            return None
