"""Playwright invocation for the black-box acceptance project.

The acceptance suite runs in its own project: its own config, its own runner, its own
temporary database. It is never mixed with the step 10 display tests or the step 12
requirement suites, so a passing acceptance run cannot be explained by their fixtures.
"""

import os
from tempfile import TemporaryDirectory

from infrastructure.stages.frontend.acceptance.journey import SPEC_ROOT
from infrastructure.stages.frontend.acceptance.runtime import RUNNER
from infrastructure.stages.frontend.ui.testing import FrontendChecks, parse_playwright_result


class AcceptanceChecks(FrontendChecks):
    """Compile and run the derived journeys against the real application."""

    def typecheck(self, files, full_source=True):
        """Compile the acceptance specs together with the application sources they drive."""
        return super().typecheck(files, test_roots=(SPEC_ROOT,),
            source_files=("src/**/*.ts", "src/**/*.tsx") if full_source else ("src/main.tsx",))

    def tests(self, files):
        """Run only the derived journeys with an owned temporary database, then clean it up."""
        if not files:
            raise ValueError("Do not run an empty acceptance suite")
        for relative in files:
            if not (self.output / relative).resolve().is_relative_to(self.output / SPEC_ROOT):
                raise ValueError("Run only the derived acceptance journeys")
        with TemporaryDirectory(prefix="arc-acceptance-owner-") as temporary:
            env = {**os.environ, "ARC_ACCEPTANCE_TEMP_ROOT": temporary}
            print("Black-box acceptance | real browser + real backend + isolated database | "
                + ", ".join(files), flush=True)
            result = self.execute([RUNNER, *files], env=env)
        return parse_playwright_result(result, include_files=True)
