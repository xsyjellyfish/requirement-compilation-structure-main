"""Generate Dify Playwright tests for arbitrary YAML requirements in small batches.

Requires: PyYAML and mini6_dify_client_streaming.py in the same directory.
No application-specific scenarios, selectors or test code are hard-coded here.

Examples:
  python -u mini6_dify_batch.py input/requirements.yaml --dry-run
  python -u mini6_dify_batch.py input/requirements.yaml --limit 1
  python -u mini6_dify_batch.py input/requirements.yaml --all
  python -u mini6_dify_batch.py input/requirements.yaml --all --context routes.txt

This ONLY generates files. Never directly runs LLM-authored tests.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    raise SystemExit("PyYAML is missing: pip install PyYAML")

from mini6_dify_client_streaming import _read_env_file, call_dify


def iter_units(document):
    """Yield (requirement node, one scenario or None, ancestor context)."""
    def walk(node, ancestors):
        if isinstance(node, list):
            for item in node:
                yield from walk(item, ancestors)
            return
        if not isinstance(node, dict):
            return
        # Accept ROOT/REQ trees and documents wrapping a requirements list.
        if 'id' not in node and 'requirements' in node:
            context = [{k: v for k, v in node.items() if k != 'requirements'}]
            yield from walk(node['requirements'], ancestors + context)
            return
        children = node.get('children', [])
        scenarios = node.get('scenarios', [])
        # Do not generate tests from a container node that has no own scenarios.
        if isinstance(scenarios, list) and scenarios:
            for scenario in scenarios:
                yield node, scenario, ancestors
        elif not children and node.get('id') and node.get('id') != 'ROOT':
            # Leaf with prose but no declared scenario: derive only supported checks.
            yield node, None, ancestors
        next_ancestors = ancestors + [
            {k: node[k] for k in ('id', 'name', 'description', 'dependencies') if k in node}
        ]
        if children:
            yield from walk(children, next_ancestors)

    yield from walk(document, [])


def make_payload(node, scenario, ancestors):
    # Parent/module descriptions and dependencies are retained, not silently shortened.
    requirement = {k: v for k, v in node.items() if k != 'children' and k != 'scenarios'}
    if scenario is not None:
        requirement['scenarios'] = [scenario]
    return yaml.safe_dump({
        'context_from_parent_requirements': ancestors,
        'current_requirement': requirement,
    }, allow_unicode=True, sort_keys=False)


def safe_filename(text):
    return re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(text)).strip('._')[:70] or 'requirement'


def run(args):
    _read_env_file(Path(__file__).resolve().parent / '.env')
    req_file = args.requirements / 'requirements.yaml' if args.requirements.is_dir() else args.requirements
    if not req_file.is_file():
        raise ValueError(f'Requirements not found: {req_file}')
    doc = yaml.safe_load(req_file.read_text(encoding='utf-8-sig'))
    units = list(iter_units(doc))
    if not units:
        raise ValueError('No requirement nodes found in YAML. Check id/children/scenarios structure.')
    context = args.context.read_text(encoding='utf-8-sig') if args.context else ''
    visual = args.visual_context.read_text(encoding='utf-8-sig') if args.visual_context else ''
    if len(context) > 100000 or len(visual) > 40000:
        raise ValueError('context or visual_context exceeds Dify input limits')
    chosen = units if args.all else units[:args.limit]
    print(f'Found {len(units)} requirement/scenario units; selected {len(chosen)}', flush=True)
    if args.dry_run:
        for index, (node, scenario, ancestors) in enumerate(chosen, 1):
            body = make_payload(node, scenario, ancestors)
            print(f'[{index}] {node.get("id", "?")} / {scenario.get("name", "?") if isinstance(scenario, dict) else "description"} | chars={len(body)}')
        return 0

    key = os.getenv('DIFY_API_KEY', '').strip()
    if not key:
        raise ValueError('DIFY_API_KEY missing from environment or .env')
    base_url = os.getenv('DIFY_API_BASE_URL', 'https://api.dify.ai/v1').strip()
    if not (base_url.startswith('https://') or base_url.startswith('http://localhost') or base_url.startswith('http://127.0.0.1')):
        raise ValueError('Dify API URL must use HTTPS (except localhost)')

    out = args.out
    (out / 'tests').mkdir(parents=True, exist_ok=True)
    (out / 'responses').mkdir(parents=True, exist_ok=True)
    records = []
    for index, (node, scenario, ancestors) in enumerate(chosen, 1):
        rid = str(node.get('id', 'UNKNOWN'))
        name = scenario.get('name', 'description') if isinstance(scenario, dict) else 'description'
        body = make_payload(node, scenario, ancestors)
        print(f'\n[{index}/{len(chosen)}] {rid} — {name} ({len(body)} chars)', flush=True)
        record = {'index': index, 'requirement_id': rid, 'scenario': name, 'status': 'error'}
        try:
            if len(body) > args.max_chars:
                raise ValueError(f'Unit input too long: {len(body)} > {args.max_chars} chars; not truncating requirements')
            response = call_dify({
                'inputs': {
                    'mode': 'generate', 'requirements': body,
                    'code_context': context, 'visual_context': visual, 'test_result': '',
                },
                'response_mode': 'streaming', 'user': 'mini6-batched-testing',
            }, key, base_url)
            filename = f'{index:04d}_{safe_filename(rid)}_{safe_filename(name)}'
            # Save structured artifacts before evaluating how many scenarios can be executed.
            (out / 'responses' / f'{filename}.json').write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding='utf-8')
            cases = response.get('cases', [])
            spec = response.get('playwright_test', '')
            if not isinstance(cases, list) or not cases or not isinstance(spec, str) or not spec.strip():
                raise ValueError('No runnable tests in Dify response')
            unexpected_ids = [c.get('requirement_id') for c in cases if c.get('requirement_id') != rid]
            if unexpected_ids:
                raise ValueError(f'Output requirement ID mismatch: {unexpected_ids} expected {rid}')
            target = out / 'tests' / f'{filename}.spec.ts'
            target.write_text(spec, encoding='utf-8')
            record.update(status='generated', cases=len(cases), file=str(target),
                          uncovered=response.get('uncovered_requirements', []),
                          skipped_tests='test.skip' in spec)
            print(f'Generated: {target} ({len(cases)} cases)', flush=True)
            if 'test.skip' in spec:
                print('WARNING: test.skip is present. Provide valid data fixtures before real acceptance.', flush=True)
        except Exception as exc:
            record['error'] = str(exc)
            print(f'ERROR: {exc}', flush=True)
        records.append(record)
        (out / 'summary.json').write_text(json.dumps({
            'source': str(req_file), 'total_units': len(units),
            'selected': len(chosen), 'completed': len(records), 'results': records,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        if record['status'] == 'error' and not args.keep_going:
            print('Stopped on first failed unit. See summary.json.', flush=True)
            return 1
    success = sum(rec['status'] == 'generated' for rec in records)
    print(f'\nFinished: {success}/{len(chosen)} generated, stored in {out.resolve()}', flush=True)
    print('Tests were NOT run. Review and execute in an isolated workspace.', flush=True)
    return int(success != len(chosen))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('requirements', type=Path)
    parser.add_argument('--context', type=Path)
    parser.add_argument('--visual-context', type=Path)
    parser.add_argument('--out', type=Path, default=Path('dify_testing_output_batch'))
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--limit', type=int, default=1, help='Number of scenario units to generate (default 1)')
    selection.add_argument('--all', action='store_true', help='Generate for all scenario units (may cause many paid calls)')
    parser.add_argument('--max-chars', type=int, default=14000, help='Do not truncate longer units; fail clearly instead')
    parser.add_argument('--dry-run', action='store_true', help='Preview units without calling Dify')
    parser.add_argument('--keep-going', action='store_true', help='Continue after a failed unit')
    args = parser.parse_args()
    if args.limit <= 0 or args.max_chars < 1000:
        parser.error('limit must be positive, max-chars >= 1000')
    try:
        return run(args)
    except Exception as exc:
        print('ERROR:', exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
