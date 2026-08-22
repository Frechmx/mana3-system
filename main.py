"""Cloud Run entry point for the longitudinal layer.

Deployed from the repository root so that `longitudinal/` ships alongside
the service module that imports it. This file is a re-export and nothing
else; the handler lives in relational/main.py and the logic it calls lives
in longitudinal/.

As the remaining analyzers are ported, their handlers are re-exported here
too and this becomes the single service's routing surface.
"""

from relational.main import compute_relational_matrix  # noqa: F401
