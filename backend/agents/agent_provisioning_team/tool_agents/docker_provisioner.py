"""
Docker container provisioner tool agent.

Handles container lifecycle: create, start, stop, remove.
"""

import logging
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from ..models import (
    AccessVerification,
    DeprovisionResult,
    GeneratedCredentials,
    ToolProvisionResult,
)
from ..shared.fencing import StaleFencingTokenError
from ..shared.provisioner_state import ProvisionerStateStore
from .base import BaseToolProvisioner

logger = logging.getLogger(__name__)


def _reports_container_absent(stderr: Optional[str]) -> bool:
    """Report whether a docker CLI error message means the container is absent.

    Preconditions:
        * ``stderr`` is the captured stderr of a docker command (or ``None``).
    Postconditions:
        * Returns ``True`` only for the daemon's specific missing-container /
          missing-object responses. Broad substrings like ``"no such"`` alone
          would also match infrastructure errors ("no such file or directory"
          from a missing daemon socket or storage path), which must NOT be
          mistaken for proof of absence.
    """
    text = (stderr or "").lower()
    return "no such container" in text or "no such object" in text


def _reports_name_conflict(stderr: Optional[str]) -> bool:
    """Report whether a ``docker run`` error is the container-name-conflict response.

    Preconditions:
        * ``stderr`` is the captured stderr of a ``docker run`` command (or ``None``).
    Postconditions:
        * Returns ``True`` only for the daemon's specific "name ... already in use
          by container" response. A broader ``"already in use"`` substring would
          also match unrelated failures — e.g. the port-bind error documented in
          ``backend/agents/docker/README.md`` (``"address already in use"``) —
          which must NOT suppress cleanup of a container this attempt actually
          created before the unrelated failure struck.
    """
    text = (stderr or "").lower()
    return "already in use by container" in text


# Every sandbox is provisioned with full access — there is no permission
# tier ladder (#456). Tool provisioners record their canonical full set
# here so onboarding docs / audit reports keep listing what the agent has.
_FULL_DOCKER_PERMISSIONS: list[str] = [
    "inspect",
    "logs",
    "exec",
    "start",
    "stop",
    "restart",
]

# Docker label stamped on every container this provisioner creates, carrying
# the Temporal job_id of the attempt that created it. This is an
# attempt-scoped identity marker that survives local idempotency-state loss:
# check_existing_environment_activity and compensate_activity (both in
# temporal/activities.py) read it back to positively tell "this run's own
# leaked container" apart from "a container that predates this run" in the
# exact scenario where no EnvironmentStore record survives to answer that on
# its own (see those activities' docstrings for the full reasoning).
JOB_ID_LABEL = "khala.job_id"

# Config-dict key provision_tool_activity/run_setup stash job_id under before
# calling DockerProvisionerTool.provision(...). Underscore-prefixed, internal
# naming so it's vanishingly unlikely to collide with a real manifest-supplied
# config key (base_image, expose_ssh, etc.) — though manifest config schemas
# allow arbitrary extra keys, so this is a naming convention, not an enforced
# guarantee. Only ever injected when the target provisioner is specifically
# "docker_provisioner" (see provision_tool_activity), since at least one
# other provisioner (generic_provisioner) echoes its whole config dict
# verbatim into persisted/returned state with no redaction for unknown keys.
JOB_ID_CONFIG_KEY = "_provisioning_job_id"


class DockerProvisionerTool(BaseToolProvisioner):
    """Tool agent for Docker container provisioning."""

    tool_name = "docker"

    def __init__(self, workspace_base: str = "/workspace") -> None:
        self.workspace_base = workspace_base
        # Persistent state: survives restarts, makes provision() idempotent.
        self._state = ProvisionerStateStore("docker_provisioner")

    def provision(
        self,
        agent_id: str,
        config: Dict[str, Any],
        credentials: GeneratedCredentials,
        fencing_token: Optional[int] = None,
    ) -> ToolProvisionResult:
        """Create and start a Docker container for the agent (idempotent)."""
        return self.run_idempotent(
            agent_id,
            credentials=credentials,
            create=lambda _register: self._do_provision(agent_id, config, credentials),
            reuse=lambda existing: self._on_reuse(agent_id, existing, credentials),
            # `_do_provision` can succeed (container created) and then the
            # idempotency-store write can still fail (full/read-only cache):
            # the store never recorded the container, so the state-lookup-based
            # `deprovision(agent_id)` path can't find it — remove it by the name
            # `_do_provision` just returned instead.
            on_persist_failure=lambda details: self._best_effort_remove_container(
                details["container_name"]
            ),
            fencing_token=fencing_token,
        )

    def _do_provision(
        self,
        agent_id: str,
        config: Dict[str, Any],
        credentials: GeneratedCredentials,
    ) -> Tuple[List[str], Dict[str, Any]]:
        container_name = f"agent-{agent_id}"
        base_image = config.get("base_image", "python:3.11-slim")
        workspace_path = config.get("workspace_path", f"{self.workspace_base}/{agent_id}")
        ssh_port = config.get("ssh_port", self._allocate_port(agent_id))

        build_cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--hostname",
            container_name,
            "-v",
            f"{workspace_path}:/workspace",
            "-w",
            "/workspace",
            "--restart",
            "unless-stopped",
        ]

        env_vars = config.get("environment", {})
        for key, value in env_vars.items():
            build_cmd.extend(["-e", f"{key}={value}"])

        # Normalized (not just truthy) so a stamped label and a later
        # equality comparison against it (DockerProvisionerTool.is_pre_existing)
        # can never disagree over incidental leading/trailing whitespace —
        # the read side already strips, so the written value must match.
        job_id = (config.get(JOB_ID_CONFIG_KEY) or "").strip() or None
        if job_id:
            # Stamped so a later, disjoint activity invocation (compensate_activity,
            # or this same job_id's own retried check_existing_environment_activity)
            # can positively attribute this container to THIS attempt even if the
            # local idempotency state that would otherwise prove it never gets
            # written — the label lives on the container itself, independent of
            # anything local (issue: self-leaked vs pre-existing disambiguation).
            build_cmd.extend(["--label", f"{JOB_ID_LABEL}={job_id}"])

        if config.get("expose_ssh", False):
            build_cmd.extend(["-p", f"{ssh_port}:22"])

        build_cmd.append(base_image)

        init_cmd = config.get("init_command", "tail -f /dev/null")
        build_cmd.extend(["sh", "-c", init_cmd])

        # A failed or timed-out `docker run` can still have created the container
        # (created/exited state, or running when the timeout fired). No state row
        # is written on failure, so deprovision-by-state could never find it —
        # best-effort remove it here, or the name blocks every future provision.
        # Only when the name verifiably did NOT exist before this attempt,
        # though: a run that failed against a pre-existing same-named container
        # (e.g. the state row was lost while the container lives on) must not
        # destroy that container — it may be a healthy agent.
        existed_before, existed_owner_job_id = self._inspect_existence_and_owner(container_name)
        if existed_before is True and job_id and existed_owner_job_id == job_id:
            # This name is blocked by a container THIS SAME job_id's own
            # earlier, orphaned attempt created (e.g. local idempotency state
            # was lost after a retried activity) — the label proves
            # self-ownership, so it's safe to reclaim before attempting
            # `docker run`, rather than hard-failing a same-job_id retry that
            # should be able to converge cleanly on its own.
            self._best_effort_remove_container(container_name)
            existed_before = False
        try:
            result = subprocess.run(
                build_cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except Exception:
            # Not just TimeoutExpired: any post-launch failure (e.g. decoding
            # the captured output) can strike after the daemon already created
            # the container, so every raising path gets the guarded cleanup.
            if existed_before is False:
                self._best_effort_remove_container(container_name)
            raise

        if result.returncode != 0:
            stderr = result.stderr or ""
            # A name-conflict failure proves this run created nothing — the
            # container belongs to whoever won the name after our probe (e.g. a
            # concurrent provision attempt) — so removal must never fire then.
            # Matched narrowly: a broad "already in use" substring also matches
            # unrelated failures (e.g. a port-bind "address already in use"),
            # which must still trigger cleanup of the container this attempt
            # actually created.
            if existed_before is False and not _reports_name_conflict(stderr):
                self._best_effort_remove_container(container_name)
            raise RuntimeError(f"Docker run failed: {stderr}")

        container_id = result.stdout.strip()[:12]
        permissions = list(_FULL_DOCKER_PERMISSIONS)

        credentials.extra["container_id"] = container_id
        credentials.extra["container_name"] = container_name
        credentials.extra["workspace_path"] = workspace_path

        details = {
            "container_id": container_id,
            "container_name": container_name,
            "ssh_port": ssh_port,
            "workspace_path": workspace_path,
            "status": "running",
        }
        return permissions, details

    def _on_reuse(
        self,
        agent_id: str,
        existing: Dict[str, Any],
        credentials: GeneratedCredentials,
    ) -> List[str]:
        """Populate credentials from stored state, after checking it isn't stale.

        Preconditions:
            * ``agent_id`` is non-empty.
            * ``existing`` is the persisted idempotency-state ``details`` dict
              for ``agent_id``.
        Postconditions:
            * When the daemon confirms ``existing``'s container no longer
              exists, the stale state row is deleted and ``RuntimeError`` is
              raised (via ``run_idempotent``'s exception handling this becomes
              an error result) — trusting it would register a "running"
              environment backed by nothing; deleting it lets a retried
              ``provision()`` recreate the container fresh instead of hitting
              the same stale row forever.
            * Otherwise (confirmed alive, or unknown because the daemon
              couldn't be reached) proceeds normally: mutates ``credentials``
              from ``existing`` and returns the full permission set. An
              *unknown* result is not treated as absence — that would risk
              tearing down tracking for a container that is actually still
              alive, the same conservative default used elsewhere in this
              provisioner.
        """
        assert agent_id, "agent_id must be non-empty"
        container_name = existing.get("container_name", "")
        if container_name and self._container_exists(container_name) is False:
            self._state.delete(agent_id)
            raise RuntimeError(
                f"container {container_name!r} no longer exists; "
                "stale idempotency state cleared for retry"
            )
        credentials.extra["container_id"] = existing.get("container_id", "")
        credentials.extra["container_name"] = existing.get("container_name", "")
        credentials.extra["workspace_path"] = existing.get("workspace_path", "")
        return list(_FULL_DOCKER_PERMISSIONS)

    def verify_access(self, agent_id: str) -> AccessVerification:
        """Verify the Docker container is reachable."""
        container_info = self._state.get(agent_id)

        if not container_info:
            return self._make_verification(
                passed=False,
                actual_permissions=[],
                errors=[f"No container found for agent {agent_id}"],
            )

        try:
            result = subprocess.run(
                ["docker", "inspect", "--type=container", container_info["container_name"]],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                return self._make_verification(
                    passed=False,
                    actual_permissions=[],
                    errors=["Container not accessible"],
                )

            return self._make_verification(
                passed=True,
                actual_permissions=list(_FULL_DOCKER_PERMISSIONS),
            )

        except Exception as e:
            return self._make_verification(
                passed=False,
                actual_permissions=[],
                errors=[str(e)],
            )

    @staticmethod
    def _run_docker_inspect(container_name: str, format_str: str) -> Optional[Tuple[int, str, str]]:
        """Run one ``docker inspect --type=container --format <format_str> <name>``.

        Shared transport for every docker-inspect-based probe on this class
        (``_container_exists``, ``_inspect_existence_and_owner``) — each
        interprets the raw result differently, but none re-implement the
        subprocess/timeout/exception envelope independently.

        Preconditions:
            * ``container_name`` is non-empty.
        Postconditions:
            * Returns ``(returncode, stdout, stderr)`` from the completed
              process on any exit code.
            * Returns ``None`` when the probe itself failed to run (daemon
              unreachable, timeout) — callers must treat this as unknown.
            * Never raises.

        ``--type=container`` restricts the probe to the container namespace:
        without it, ``docker inspect NAME`` also matches images, networks, or
        volumes sharing that name, which would report a match for a name no
        *container* actually holds.
        """
        assert container_name, "container_name must be non-empty"
        try:
            probe = subprocess.run(
                ["docker", "inspect", "--type=container", "--format", format_str, container_name],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:  # noqa: BLE001 — probe is advisory only
            return None
        return probe.returncode, probe.stdout, probe.stderr

    @staticmethod
    def _container_exists(container_name: str) -> Optional[bool]:
        """Probe whether a container named ``container_name`` currently exists.

        Preconditions:
            * ``container_name`` is non-empty.
        Postconditions:
            * Returns ``True`` when the daemon reports a *container* with this
              name, ``False`` when the daemon reports it absent, and ``None``
              when the probe itself failed (daemon unreachable, timeout) —
              callers must treat ``None`` as unknown and act conservatively.
            * Never raises.
        """
        probe = DockerProvisionerTool._run_docker_inspect(container_name, "{{.Id}}")
        if probe is None:
            return None
        returncode, _stdout, stderr = probe
        if returncode == 0:
            return True
        if _reports_container_absent(stderr):
            return False
        return None

    @staticmethod
    def _inspect_existence_and_owner(container_name: str) -> Tuple[Optional[bool], Optional[str]]:
        """Probe existence and the ``khala.job_id`` label in one ``docker inspect`` call.

        Preconditions:
            * ``container_name`` is non-empty.
        Postconditions:
            * Returns ``(exists, owner_job_id)``: ``exists`` follows
              ``_container_exists``'s own tri-state contract (``True``/
              ``False``/``None``); ``owner_job_id`` is the label's value when
              the daemon reports one non-empty, else ``None`` (absent
              container, absent/empty label — Docker's Go-template ``index``
              on a missing map key yields ``""``, not an error, the same
              behavior already relied on by ``sandbox/provisioner.py``'s read
              of docker-compose's own ``com.docker.compose.project`` label —
              or inconclusive probe). ``owner_job_id`` is only ever
              meaningful when ``exists`` is ``True``.
            * A single subprocess call answers both questions, instead of two
              sequential ``docker inspect`` round-trips.
            * Never raises.
        """
        probe = DockerProvisionerTool._run_docker_inspect(
            container_name, f'{{{{.Id}}}}|||{{{{ index .Config.Labels "{JOB_ID_LABEL}" }}}}'
        )
        if probe is None:
            return None, None
        returncode, stdout, stderr = probe
        if returncode != 0:
            if _reports_container_absent(stderr):
                return False, None
            return None, None
        _id_part, _sep, label_part = stdout.partition("|||")
        return True, (label_part.strip() or None)

    @staticmethod
    def is_pre_existing(agent_id: str, job_id: Optional[str]) -> bool:
        """Report whether ``agent-{agent_id}``'s container might predate this run.

        The single, shared entry point ``check_existing_environment_activity``
        and ``compensate_activity`` (``temporal/activities.py``) both call
        once no ``EnvironmentStore`` record can answer the question on its
        own: a container matching the deterministic name is ambiguous by
        name alone, since one this same run's own setup phase created and
        failed to clean up locally looks identical, by name, to one that
        genuinely predates this run. The ``khala.job_id`` label (stamped at
        creation time, see ``JOB_ID_LABEL``) resolves that ambiguity.

        Preconditions:
            * ``agent_id`` is non-empty.
            * ``job_id``, when given, is the calling run's own job id.
        Postconditions:
            * Returns ``False`` when the container is confirmed absent —
              nothing to protect or attribute to any run.
            * Returns ``False`` when the container exists (or its existence
              is inconclusive) and its label is confirmed equal to
              ``job_id`` — unambiguously this run's own container.
            * Returns ``True`` (conservative) otherwise: the container exists
              or its existence is inconclusive, and either ``job_id`` wasn't
              given to compare against, the label is absent, or the label
              names a different job. A different job's label is logged
              (informational, not alarming) — the per-agent_id ownership
              lock ordinarily prevents a live *concurrent* job from creating
              one here, so a foreign label most often means a distinct,
              later job legitimately re-provisioned this same ``agent_id``
              after this run's own lock was released (e.g. a re-run against
              an already-delivered agent), not a lock violation.
            * Never raises.
        """
        assert agent_id, "agent_id must be non-empty"
        exists, owner_job_id = DockerProvisionerTool._inspect_existence_and_owner(
            f"agent-{agent_id}"
        )
        if exists is False:
            return False
        if job_id and owner_job_id == job_id:
            return False
        if job_id and owner_job_id:
            logger.info(
                "agent_id=%r container is labeled with a different job (owner=%r, this "
                "run=%r) — likely a later, separate run against the same agent_id after "
                "this run's own ownership lock was released; treating it as possibly "
                "pre-existing",
                agent_id,
                owner_job_id,
                job_id,
            )
        return True

    @staticmethod
    def _best_effort_remove_container(container_name: str) -> None:
        """Remove a container left behind by a failed ``docker run``, by name.

        Preconditions:
            * ``container_name`` is non-empty.
        Postconditions:
            * ``docker rm -f`` was attempted; failures — a nonzero exit as well
              as a raising call (daemon down, timeout) — are logged and
              swallowed, so this cleanup can never mask the provisioning error
              that triggered it. A container the daemon already reports absent
              counts as removed.
        """
        assert container_name, "container_name must be non-empty"
        try:
            removal = subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            stderr = removal.stderr or ""
            if removal.returncode != 0 and not _reports_container_absent(stderr):
                logger.error(
                    "Best-effort removal of partially created container %s exited "
                    "nonzero; container may survive and block reprovisioning: %s",
                    container_name,
                    stderr.strip() or removal.returncode,
                )
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.exception(
                "Best-effort removal of partially created container %s failed",
                container_name,
            )

    def verify_and_remove_orphan(self, agent_id: str) -> bool:
        """Best-effort verify-and-remove by deterministic name, ignoring state.

        A container can exist with no corresponding ``self._state`` row (e.g.
        ``docker run`` succeeded but persisting provisioner state then failed,
        and the best-effort removal that follows that specific failure also
        failed) — ``deprovision``'s state-lookup-based teardown can never find
        such a container, since there is no row to look up. This probes and
        removes by the deterministic ``agent-{agent_id}`` name directly,
        independent of whatever ``self._state`` currently holds (or doesn't).

        Preconditions:
            * ``agent_id`` is non-empty.
        Postconditions:
            * Returns ``True`` iff the daemon confirms no container by this
              name exists after this call — already absent, or successfully
              removed just now.
            * Returns ``False`` when the daemon confirms the container is
              still alive after a removal attempt, or when either probe is
              inconclusive (daemon unreachable/timeout) — conservative, since
              "unknown" must not be reported as confirmed teardown.
            * Never raises.
        """
        assert agent_id, "agent_id must be non-empty"
        container_name = f"agent-{agent_id}"
        if self._container_exists(container_name) is False:
            return True
        self._best_effort_remove_container(container_name)
        return self._container_exists(container_name) is False

    def deprovision(self, agent_id: str, fencing_token: Optional[int] = None) -> DeprovisionResult:
        """Stop and remove the Docker container.

        ``fencing_token``, when given, is checked *before* the real
        ``docker stop``/``docker rm`` calls run, not just before the final
        state persist, so a stale caller is rejected before it can touch the
        live container.
        """
        if fencing_token is not None:
            self._state.check_fencing_token(agent_id, fencing_token)

        container_info = self._state.get(agent_id)

        if not container_info:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={"message": "No container to remove"},
            )

        try:
            container_name = container_info["container_name"]

            # Best-effort stop; `rm -f` below removes a running container anyway.
            subprocess.run(
                ["docker", "stop", container_name],
                capture_output=True,
                timeout=60,
            )

            removal = subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True,
                text=True,
                timeout=30,
            )
            stderr = removal.stderr or ""
            if removal.returncode != 0 and not _reports_container_absent(stderr):
                # An ambiguous CLI failure (e.g. the daemon removed the
                # container but the response/connection was then lost) does not
                # by itself prove the container survived — a follow-up existence
                # probe disambiguates. Only when the container is CONFIRMED gone
                # do we clear state; an unknown or confirmed-alive result keeps
                # the state row, since deleting it while the container survives
                # would leave it untracked and its name blocking reprovisioning.
                if self._container_exists(container_name) is False:
                    self._state.delete(agent_id, fencing_token=fencing_token)
                    return DeprovisionResult(
                        tool_name=self.tool_name,
                        success=True,
                        details={
                            "container_removed": container_name,
                            "message": "rm reported failure but daemon confirms removed",
                        },
                    )
                return DeprovisionResult(
                    tool_name=self.tool_name,
                    success=False,
                    error=f"docker rm failed: {stderr.strip() or removal.returncode}",
                )

            self._state.delete(agent_id, fencing_token=fencing_token)

            return DeprovisionResult(
                tool_name=self.tool_name,
                success=True,
                details={"container_removed": container_name},
            )

        except StaleFencingTokenError:
            # A stale-token rejection from the fenced _state.delete is an
            # ownership error, not an infra failure: propagate it (non-retryable)
            # instead of folding it into a soft success=False result.
            raise
        except Exception as e:
            return DeprovisionResult(
                tool_name=self.tool_name,
                success=False,
                error=str(e),
            )

    def _allocate_port(self, agent_id: str) -> int:
        """Allocate an SSH port for the container."""
        base_port = 22000
        offset = abs(hash(agent_id)) % 1000
        return base_port + offset

    def get_container_info(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get container information for an agent."""
        return self._state.get(agent_id)

    def exec_in_container(
        self,
        agent_id: str,
        command: List[str],
        timeout: int = 60,
    ) -> Tuple[int, str, str]:
        """Execute a command inside the container.

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        container_info = self._state.get(agent_id)
        if not container_info:
            return 1, "", f"No container for agent {agent_id}"

        try:
            result = subprocess.run(
                ["docker", "exec", container_info["container_name"]] + command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return 1, "", "Command timed out"
        except Exception as e:
            return 1, "", str(e)
