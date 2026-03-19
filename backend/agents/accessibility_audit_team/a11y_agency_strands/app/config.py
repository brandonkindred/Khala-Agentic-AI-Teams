from dataclasses import dataclass


@dataclass(slots=True)
class AgencyConfig:
    """Runtime configuration for the deterministic workflow coordinator."""

    artifact_root: str = "./.a11y_artifacts"
    require_human_approval: bool = True
    use_s3_session_manager: bool = False
