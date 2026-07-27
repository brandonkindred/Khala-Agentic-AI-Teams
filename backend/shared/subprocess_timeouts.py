"""Named, env-overridable subprocess timeout constants for devops tool-agents.

Centralizes the per-tool timeout values that the devops team's stateless
tool-agents (IaC validation, dry-run, terraform/helm/cdk/docker-compose
execution, policy-as-code, and the enterprise architect subprocess) pass to
``subprocess``/``shared.command_runner.executor.run_command``. Each constant is
read once at import time via the shared ``env_int`` reader (see
``shared.env_config``), so a slow CI runner can raise any single tool's
timeout without a code change, while an unset or garbage env value falls back
to the documented default rather than raising.

Invariants:
    - Every constant is a positive int, in seconds.
"""

from __future__ import annotations

from shared.env_config import env_int

# Helm dry-run (`helm lint .`) in the deployment dry-run tool-agent.
DEVOPS_HELM_DRY_RUN_TIMEOUT_S = env_int("DEVOPS_HELM_DRY_RUN_TIMEOUT_S", 120, floor=1)

# `terraform init`/`validate`/`plan`/`apply`/`fmt` in the terraform execution tool-agent.
DEVOPS_TERRAFORM_EXECUTION_TIMEOUT_S = env_int("DEVOPS_TERRAFORM_EXECUTION_TIMEOUT_S", 180, floor=1)

# `helm template`/`lint` (read-only) in the helm execution tool-agent.
DEVOPS_HELM_EXECUTION_TIMEOUT_S = env_int("DEVOPS_HELM_EXECUTION_TIMEOUT_S", 120, floor=1)

# `cdk synth`/`cdk diff` (read-only) in the CDK execution tool-agent.
DEVOPS_CDK_EXECUTION_TIMEOUT_S = env_int("DEVOPS_CDK_EXECUTION_TIMEOUT_S", 180, floor=1)

# `terraform fmt -check` and `terraform validate` in the IaC validation tool-agent.
DEVOPS_IAC_VALIDATION_TIMEOUT_S = env_int("DEVOPS_IAC_VALIDATION_TIMEOUT_S", 120, floor=1)

# `docker compose config`/`build`/`ps`/`logs` (read-only) in the docker compose execution tool-agent.
DEVOPS_DOCKER_COMPOSE_TIMEOUT_S = env_int("DEVOPS_DOCKER_COMPOSE_TIMEOUT_S", 120, floor=1)

# Checkov scan in the policy-as-code tool-agent.
DEVOPS_POLICY_AS_CODE_TIMEOUT_S = env_int("DEVOPS_POLICY_AS_CODE_TIMEOUT_S", 180, floor=1)

# `architect_agents/main.py` subprocess invoked from the integration helper.
DEVOPS_ARCHITECT_INTEGRATION_TIMEOUT_S = env_int("DEVOPS_ARCHITECT_INTEGRATION_TIMEOUT_S", 3600, floor=1)
