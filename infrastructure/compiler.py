"""Run the complete requirement compiler in one process using the existing passes."""

from importlib import import_module
from pathlib import Path

from infrastructure.reporting import final, status as run
from infrastructure.shared.agent.loop import AgentBlocked
from infrastructure.stages.database.acceptance import (
    DATABASE_STAGE, SchemaBlocked, repair_request, upstream_repair)
from infrastructure.utils.presentation.console import show_panel
from infrastructure.utils.presentation.logging import console_log


class Compiler:
    """Own shared paths, configuration, and results for one compilation."""

    def __init__(self, input_dir: Path, output_dir: Path, model_log: bool = False, port: int = 3000) -> None:
        if not 1 <= port <= 65535:
            raise ValueError("Backend port must be between 1 and 65535")
        self.input_dir = Path(input_dir).expanduser().resolve()
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.arc_dir = self.output_dir / ".arc"
        self.model_log = model_log
        self.port = port
        self.artifacts: dict[str, object] = {}
        self.current_stage: str | None = None
        self.report = {"status": run.NOT_STARTED, "stages": {}}

    def _log_offset(self) -> int:
        """Count the log lines already written, so one pass can report its own range."""
        try:
            return len((self.output_dir / final.LOG).read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            return 0

    def _log_range(self, module: str, offset: int) -> dict:
        """Point the report at the compiler.log lines this pass appended."""
        try:
            lines = (self.output_dir / final.LOG).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return {}
        marker = "[START] Stage " + module[:2]
        start = next((number for number in range(len(lines) - 1, offset - 1, -1)
            if marker in lines[number]), offset + 1)
        return {"file": final.LOG, "start_line": start, "end_line": len(lines)}

    def _save(self) -> None:
        """Persist the in-progress report after every pass so a crash still leaves evidence."""
        final.build_report(self.report, output_dir=self.output_dir, arc_dir=self.arc_dir)
        final.write_report(self.arc_dir, self.report)

    def _run(self, module: str, function: str, *arguments, **options):
        """Call a pass directly, recording whether its failure was fatal or isolated."""
        self.current_stage = module + ".py"
        started, offset = final.utc_now(), self._log_offset()
        with console_log(self.output_dir, stage=self.current_stage,
                model_log=self.model_log, debug_model_io=self.model_log):
            try:
                result = getattr(import_module(module), function)(*arguments, **options)
            except BaseException as error:
                # An escaped exception is never recoverable: the pass did not finish, so the
                # application cannot be completed. A failure that the pass itself reported as
                # FAILED (a failed test, a rejected contract) is isolated and handled below.
                interrupted = isinstance(error, KeyboardInterrupt)
                self.report["status"] = run.INTERRUPTED if interrupted else run.FAILED
                self.report["stages"][module] = final.stage_record(module, None,
                    status=self.report["status"], error=error, fatal=True,
                    started=started, log=self._log_range(module, offset))
                self._save()
                raise
            self.report["stages"][module] = final.stage_record(module, result,
                started=started, log=self._log_range(module, offset))
            self._save()
            return result

    def _upstream_repair(self, error) -> dict[str, dict]:
        """Return the Step 05 repair map a blocked pass requested, or an empty map.

        A pass that read the SQLite structure reports the text that named the cause. That
        text only justifies a schema repair when the published SQL still refuses a legal
        record; otherwise the cause is outside the database (a read-only file, for example)
        and rewriting the schema would spend model calls without changing the outcome.
        """
        upstream = getattr(error, "upstream", None) or {}
        if upstream.get("problems"):
            return repair_request(upstream)
        return upstream_repair(self.arc_dir, self.output_dir, upstream.get("evidence", ""))

    def _run_repairable(self, module: str, function: str, *arguments, **options):
        """Run a pass; when it is blocked by the schema, let Step 05 repair it and retry once.

        A pass may read ``backend/src/db`` but never write it, so a database defect it finds
        can only be repaired by the pass that owns that directory. Every failure that names
        no repairable table still propagates unchanged: this adds the one repair the
        pipeline can act on, and one retry that can observe its effect.
        """
        try:
            return self._run(module, function, *arguments, **options)
        except (AgentBlocked, SchemaBlocked) as error:
            repair = self._upstream_repair(error)
            if not repair:
                raise
            print(f"{module} is blocked by {DATABASE_STAGE}: repairing "
                + ", ".join(sorted(repair)) + " and retrying the pass.", flush=True)
            self._run(DATABASE_STAGE, "implement_db", self.arc_dir, repair=repair)
            # The blocked attempt marked the run FAILED. The repair removed that cause, so the
            # retry starts again from RUNNING and only its own result can decide the status.
            self.report["status"] = run.RUNNING
            return self._run(module, function, *arguments, **options)

    def compile(self) -> Path:
        """Execute the 13 compilation passes in order and return the generated application directory."""
        self.artifacts.clear()
        self.report = {"status": run.RUNNING, "started_at": final.utc_now(), "stages": {},
            "passes": 13}
        show_panel("Requirement compiler | Pipeline",
            f"Input: {self.input_dir}\nOutput: {self.output_dir}\nPasses: 13\nDetailed model logs: {'enabled' if self.model_log else 'disabled'}")
        self.artifacts["project_files"] = self._run(
            "00_project_initialization", "project_initialization", self.output_dir, port=self.port)
        self.artifacts["requirements"] = self._run(
            "01_explore_requirement", "explore_requirement", self.input_dir, self.arc_dir)
        self.artifacts["db_schema"] = self._run(
            "02_build_db_schema", "build_db_schema", self.arc_dir)
        self.artifacts["db_obligations"] = self._run(
            "03_infer_db_obligations", "infer_db_obligations", self.arc_dir)
        self.artifacts["db_obligations"] = self._run(
            "04_check_db_obligations", "check_db_obligations", self.arc_dir)
        self.artifacts["db_implementations"] = self._run(
            "05_implement_db", "implement_db", self.arc_dir)
        self.artifacts["backend_apis"] = self._run(
            "06_design_backend_apis", "design_backend_apis", self.arc_dir)
        self.artifacts["backend_apis"] = self._run(
            "07_check_backend_apis", "check_backend_apis", self.arc_dir)
        self.artifacts["backend_checks"] = self._run_repairable(
            "08_implement_backend_tdd", "implement_backend_tdd", self.arc_dir)
        self.artifacts["ui_design"] = self._run(
            "09_design_ui", "design_frontend_ui", self.input_dir, self.arc_dir)
        self.artifacts["ui_checks"] = self._run_repairable(
            "10_implement_ui", "implement_frontend_ui", self.input_dir, self.arc_dir)
        self.artifacts["frontend_triggers"] = self._run(
            "11_design_frontend_triggers", "design_frontend_triggers", self.input_dir, self.arc_dir)
        self.artifacts["frontend_e2e"] = self._run_repairable(
            "12_implement_frontend_tdd", "implement_frontend_triggers", self.input_dir, self.arc_dir)
        self.current_stage = None
        self._save()
        return self.output_dir

