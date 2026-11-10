from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from arc_adapter import (
    MULTIMODAL_REGEX,
    UPSTREAM_INSTANCE_SKELETON,
    _model_kwargs,
    _slim_image,
    _token_totals,
    _upstream_config,
    build_task,
    inspect_input,
    prepare_output,
    run_agent,
)


@pytest.fixture
def sample(tmp_path):
    inp = tmp_path / "task"; inp.mkdir()
    (inp / "requirements.yaml").write_text('id: ROOT\nname: "Example"\ndescription:\n  text: "<important> keep & YAML"\nchildren: []\n', encoding="utf-8")
    (inp / "reference").mkdir()
    # Real tiny 1x1 PNG for image pipeline testing.
    import base64
    (inp / "reference" / "home.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=="))
    (inp / "reference" / "notes.txt").write_text("extra file", encoding="utf-8")
    return inp


def test_inspect_preserves_exact_requirement_text(sample):
    data = inspect_input(sample)
    assert data["raw_yaml"] == (sample / "requirements.yaml").read_text()
    assert len(data["images"]) == 1


def test_stage_has_web_template_and_all_original_assets(sample, tmp_path):
    output = tmp_path / "generated"
    checked = prepare_output(sample, output)
    for rel in ["package.json", "deploy.sh", "frontend/src/App.tsx", "backend/src/app.ts"]:
        assert (output / rel).is_file(), rel
    assert (output / ".arc_requirements" / "requirements.yaml").read_bytes() == (sample / "requirements.yaml").read_bytes()
    assert (output / ".arc_requirements" / "reference/home.png").read_bytes() == (sample / "reference/home.png").read_bytes()
    assert (output / ".arc_requirements" / "reference/notes.txt").read_text() == "extra file"
    assert len(checked["images"]) == 1


def test_multimodal_task_includes_original_constraints_and_image(sample):
    data = inspect_input(sample)
    task = build_task(data, sample)
    assert data["raw_yaml"] in task
    assert task.count("<MSWEA_MULTIMODAL_CONTENT>") == 1
    assert "data:image/png;base64," in task
    import re
    m = re.findall(MULTIMODAL_REGEX, task)
    assert len(m) == 1 and m[0][0] == "image_url"


def test_missing_yaml_fails(tmp_path):
    with pytest.raises(ValueError, match="Missing requirements.yaml"):
        inspect_input(tmp_path)


def test_existing_platform_files_preserved(sample, tmp_path):
    dest = tmp_path / "already-present"; dest.mkdir()
    (dest / "old.txt").write_text("do not delete")
    (dest / ".arc").mkdir()
    (dest / ".arc/stdout.log").write_text("runner log")
    prepare_output(sample, dest)
    assert (dest / ".arc/stdout.log").read_text() == "runner log"
    assert (dest / "backend/package.json").is_file()
    assert (dest / "old.txt").read_text() == "do not delete"


def test_dry_run_cli_no_api(sample, tmp_path):
    from conftest import run_entry
    p = run_entry([sample, "--output-dir", tmp_path / "site", "--dry-run"])
    assert p.returncode == 0, p.stderr
    lines = [line for line in p.stdout.splitlines() if line.strip()]
    msg = json.loads(lines[-1])  # last JSON line: the dry-run summary (earlier lines are events)
    assert msg["status"] == "dry_run_only_no_generation"
    assert msg["multimodal_image_parts"] == 1


def test_missing_api_preflight_does_not_touch_output(sample, tmp_path):
    from conftest import run_entry
    dst = tmp_path / "site"
    p = run_entry([sample, "--output-dir", dst],
                  {"OPENAI_API_KEY": "", "OPENAI_BASE_URL": ""})
    assert p.returncode == 1
    assert "OPENAI_BASE_URL and OPENAI_API_KEY" in p.stderr
    assert not dst.exists()


def test_token_totals_from_records():
    assert _token_totals([]) == {"input_tokens": None, "output_tokens": None}
    assert _token_totals([{"usage": {"prompt_tokens": 40, "completion_tokens": 9}},
                          {"usage": None},
                          {"error": [{"type": "X", "message": "boom"}]}]) == {
        "input_tokens": 40, "output_tokens": 9}


def test_model_kwargs_reasoning_effort(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.delenv("ARC_REASONING_EFFORT", raising=False)
    assert _model_kwargs(600)["reasoning_effort"] == "low"  # default
    monkeypatch.setenv("ARC_REASONING_EFFORT", "high")
    assert _model_kwargs(600)["reasoning_effort"] == "high"
    monkeypatch.setenv("ARC_REASONING_EFFORT", "")  # empty restores the endpoint default
    assert "reasoning_effort" not in _model_kwargs(600)


def test_mini_engine_wiring_without_network(sample, tmp_path, monkeypatch):
    """Mock boundary test only; does NOT count as a real model call / site generation."""
    monkeypatch.delenv("PAGER", raising=False)  # upstream env injection restores it below
    checked = inspect_input(sample)
    output = tmp_path / "site"
    prepare_output(sample, output)
    calls = {}

    class StubModel:
        _call_records = [{"request": 1, "usage": {"prompt_tokens": 12, "completion_tokens": 3}}]

        def __init__(self, **kwargs): calls["model"] = kwargs

    class StubEnvironment:
        def __init__(self, **kwargs): calls["env"] = kwargs

    class StubAgent:
        def __init__(self, model, env, **kwargs):
            calls["agent"] = kwargs
            self.messages = [{"extra": {"response": {"usage": {"prompt_tokens": 12, "completion_tokens": 3}}}}]
            self.cost = 0.0
            self.n_calls = 1

        def run(self, task):
            calls["task"] = task
            return {"exit_status": "Submitted"}

    def install(name, cls):
        m = types.ModuleType(name)
        setattr(m, cls.__name__.replace("Stub", ""), cls)
        monkeypatch.setitem(sys.modules, name, m)

    # Set explicit public API imports (the production code imports these classes lazily).
    for name, attr, klass in [
        ("minisweagent.agents.default", "DefaultAgent", StubAgent),
        ("minisweagent.environments.local", "LocalEnvironment", StubEnvironment),
        ("minisweagent.models.litellm_model", "LitellmModel", StubModel),
    ]:
        mod = types.ModuleType(name)
        setattr(mod, attr, klass)
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "canary-key-not-for-network")
    report = run_agent(checked, sample, output, "demo", 10, 50)
    assert report["status"] == "Submitted"
    assert report["llm_calls"] == 1
    assert report["model_http_attempts"] == 1
    assert report["input_tokens"] == 12 and report["output_tokens"] == 3
    assert report["actual_api_cost_usd"] is None
    assert calls["model"]["model_name"] == "openai/demo"
    assert "api_key" not in calls["model"]["model_kwargs"]
    assert calls["model"]["model_kwargs"]["timeout"] == 600
    assert calls["model"]["model_kwargs"]["num_retries"] == 0
    assert calls["model"]["model_kwargs"]["reasoning_effort"] == "low"
    assert calls["model"]["model_kwargs"]["drop_params"] is True
    assert calls["model"]["model_kwargs"]["parallel_tool_calls"] is True
    assert "elided_chars" in calls["model"]["observation_template"]  # upstream truncation policy
    assert calls["env"]["cwd"] == str(output)
    assert "<MSWEA_MULTIMODAL_CONTENT>" in calls["task"]
    assert calls["agent"]["system_template"] == _upstream_config("benchmarks/swebench.yaml")["agent"]["system_template"]
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in calls["agent"]["instance_template"]
    assert os.environ.get("PAGER") == "cat"  # upstream default.yaml shell env injected via setdefault
    assert (output / ".arc_adapter" / "run_summary.json").is_file()


def test_upstream_adoption():
    """Harness prompts/limits come from shipped upstream configs, not hand-written text."""
    upstream = _upstream_config("benchmarks/swebench.yaml")
    assert "interact with a computer shell" in upstream["agent"]["system_template"]
    for fragment in ("COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "Command Execution Rules",
                     "MULTIPLE tool calls", "deploy.sh", "Recommended Workflow"):
        assert fragment in UPSTREAM_INSTANCE_SKELETON
    # ARC-specific limits that intentionally override upstream defaults.
    assert upstream["agent"]["step_limit"] == 250


def test_elide_images_placeholder_verifiable_and_stable():
    from progress import elide_images
    raw = ('data:image/png;base64,'
           'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg==')
    out = elide_images('see ' + raw + ' end')
    assert 'iVBORw0KGgo' not in out
    assert out.startswith('see data:image/png;base64,[elided sha256=') and out.endswith(' end')
    assert elide_images(out) == out  # the placeholder itself is never re-matched


def test_slim_image_resizes_and_shrinks(tmp_path):
    from io import BytesIO

    from PIL import Image
    big = tmp_path / "big.png"
    Image.new("RGB", (3000, 2000), (120, 140, 160)).save(big, format="PNG", optimize=True)
    result = _slim_image(big)
    assert result is not None
    payload, media = result
    with Image.open(BytesIO(payload)) as out:
        assert max(out.size) <= 1280
    assert media in ("image/jpeg", "image/png")
    assert len(payload) < big.stat().st_size


def test_slim_image_falls_back_to_none_without_pillow(monkeypatch, tmp_path):
    import base64
    import sys
    png = tmp_path / "tiny.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=="))
    monkeypatch.setitem(sys.modules, "PIL", None)
    assert _slim_image(png) is None  # caller attaches the original bytes, as before


def test_local_config_keeps_platform_values(tmp_path, monkeypatch):
    from dotenv import load_dotenv
    config = tmp_path / '.env'
    config.write_text('ARC_MODEL=local\nOPENAI_BASE_URL=https://local.invalid/v1\n')
    monkeypatch.setenv('ARC_MODEL', 'platform')
    monkeypatch.delenv('OPENAI_BASE_URL', raising=False)
    load_dotenv(config, override=False)
    assert os.environ['ARC_MODEL'] == 'platform'
    assert os.environ['OPENAI_BASE_URL'] == 'https://local.invalid/v1'


def test_reentry_preserves_application_sources(sample, tmp_path):
    dest = tmp_path / 'template'
    prepare_output(sample, dest)
    app = dest / 'frontend/src/App.tsx'
    app.write_text('// existing application')
    prepare_output(sample, dest)
    assert app.read_text() == '// existing application'


def test_model_environment_alias(monkeypatch):
    from main import parse_args
    monkeypatch.delenv('ARC_MODEL', raising=False)
    monkeypatch.setenv('MODEL', 'platform-model')
    args = ['input', '--output-dir', 'output']
    assert parse_args(args).model == 'platform-model'
    monkeypatch.setenv('ARC_MODEL', 'adapter-model')
    assert parse_args(args).model == 'adapter-model'


def test_template_rejects_escape(sample, tmp_path):
    dest = tmp_path / 'template'
    dest.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (dest / 'backend').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='escapes output'):
        prepare_output(sample, dest)
