"""Shared, profile-parameterized implementations of the code-v2 lifecycle phases.

Each module here holds the logic that is common to ``backend_code_v2_team`` and
``frontend_code_v2_team``. The teams' own ``phases/*.py`` modules delegate to
these ``*_impl`` functions, injecting their team-local models, prompts, and
:class:`~software_engineering_team.shared.stack_profile.StackProfile`.
"""
