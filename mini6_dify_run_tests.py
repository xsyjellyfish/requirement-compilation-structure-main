"""Run dynamically generated Dify Playwright tests against a *running local* mini6 app.

The runner contains NO application-specific selectors, cases, routes, or expected values.
It makes a fresh Playwright project, runs tests with one worker, and saves a JSON report.

Examples (Git Bash / PowerShell):
  python mini6_dify_run_tests.py  # inspection only
  python -u mini6_dify_run_tests.py --run --url http://127.0.0.1:3000
  python -u mini6_dify_run_tests.py --run --analyze --requirements input/requirements.yaml

IMPORTANT: LLM-generated TypeScript is executable code. An isolated work folder and a
filtered environment are NOT a security sandbox. Only use --run in a disposable VM or
container when the generated code is not fully trusted; never point at production data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parent
PLAYWRIGHT_VERSION = "1.65.0"


def safe_env(base_url: str) -> dict[str, str]:
    """Don't give a generated test the Dify/OpenAI tokens from the parent shell."""
    allow = {
        'PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'WINDIR', 'APPDATA',
        'LOCALAPPDATA', 'TEMP', 'TMP', 'TMPDIR', 'COMSPEC', 'PATHEXT',
        'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432', 'HOMEDRIVE',
        'HOMEPATH', 'LANG', 'LC_ALL', 'CI', 'PLAYWRIGHT_BROWSERS_PATH',
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in allow}
    env['TEST_BASE_URL'] = base_url
    env['CI'] = '1'
    return env


def confirm_local_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'}:
        raise ValueError('For safety, --url must use http://127.0.0.1 or http://localhost')
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('--url must be the local application origin, without a path or credentials')
    return value.rstrip('/')


def check_server(url: str) -> None:
    try:
        with urlopen(Request(url + '/', headers={'User-Agent': 'mini6-test-runner/1'}), timeout=8) as resp:
            print('应用已响应：HTTP', resp.status, flush=True)
    except HTTPError as exc:
        print(f'应用有响应，但首页返回 HTTP {exc.code}；测试仍可能运行。', flush=True)
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            f'无法连接测试网站 {url}。请先在另一个终端进入生成的网站目录，运行 bash deploy.sh。原因: {exc}'
        ) from exc


def find_specs(source: Path) -> list[Path]:
    if source.is_file():
        candidates = [source]
    elif source.is_dir():
        candidates = sorted(p for p in source.rglob('*') if p.is_file() and
                            (p.name.endswith('.spec.ts') or p.name.endswith('.test.ts')))
    else:
        raise ValueError(f'找不到测试路径：{source}')
    if not candidates:
        raise ValueError(f'没有 .spec.ts 或 .test.ts：{source}')
    return candidates


def inspect_specs(paths: list[Path]) -> list[str]:
    messages = []
    for path in paths:
        text = path.read_text(encoding='utf-8-sig')
        if not re.search(r'\btest\s*\(', text):
            messages.append(f'{path.name}: 没找到 test(...)')
        if not re.search(r'\bexpect\s*(?:\(|\.poll\s*\()', text):
            messages.append(f'{path.name}: 没找到 expect 业务断言')
        if re.search(r'\btest\.only\s*\(', text):
            messages.append(f'{path.name}: 使用了 test.only，拒绝执行')
        if re.search(r'\btest\.(?:skip|fixme)\s*\(', text):
            messages.append(f'{path.name}: 包含跳过测试，不能把全部场景当作通过')
    return messages


def create_runtime(out_dir: Path, specs: list[Path], url: str) -> Path:
    runtime = out_dir / 'runtime'
    test_dir = runtime / 'tests'
    test_dir.mkdir(parents=True)
    (runtime / 'package.json').write_text(json.dumps({
        'name': 'mini6-dify-generated-tests',
        'private': True,
        'version': '1.0.0',
        'type': 'commonjs',
        'devDependencies': {'@playwright/test': PLAYWRIGHT_VERSION},
    }, indent=2), encoding='utf-8')
    config = """const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({
  testDir: './tests',
  timeout: 30000,
  globalTimeout: 240000,
  expect: { timeout: 5000 },
  workers: 1,
  retries: 0,
  fullyParallel: false,
  forbidOnly: true,
  reporter: [['line'], ['json', { outputFile: 'playwright-report.json' }]],
  outputDir: 'test-results',
  use: {
    baseURL: process.env.TEST_BASE_URL,
    headless: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
});
"""
    (runtime / 'playwright.config.cjs').write_text(config, encoding='utf-8')
    for i, spec in enumerate(specs, start=1):
        # Do not copy .env files, local project source, or arbitrary sibling imports.
        # Untrusted generated code will run in this dedicated temporary folder.
        destination = test_dir / f'{i:04d}_{spec.name}'
        shutil.copyfile(spec, destination)
    (out_dir / 'source_specs.json').write_text(json.dumps([str(s.resolve()) for s in specs],
                                                  ensure_ascii=False, indent=2), encoding='utf-8')
    return runtime


def run_command(command: list[str], cwd: Path, env: dict[str, str], timeout: int,
                log_path: Path) -> int:
    print('执行：', ' '.join(command), flush=True)
    with log_path.open('w', encoding='utf-8', errors='replace') as log:
        try:
            process = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                     timeout=timeout, check=False, shell=False)
        except subprocess.TimeoutExpired:
            log.write(f'\nCOMMAND TIMEOUT after {timeout}s\n')
            return 124
    print(f'退出码：{process.returncode}，日志：{log_path}', flush=True)
    return process.returncode


def collect_tests(data: dict) -> list[dict]:
    cases: list[dict] = []
    def descend(suites: list[dict]):
        for suite in suites:
            for spec in suite.get('specs') or []:
                for test in spec.get('tests') or []:
                    result = (test.get('results') or [{}])[-1]
                    error = '\n'.join(str(e.get('message', '')) for e in result.get('errors', [])
                                      if isinstance(e, dict))
                    cases.append({
                        'name': spec.get('title'), 'status': test.get('status'),
                        'result': result.get('status'), 'error': error[:2500],
                    })
            descend(suite.get('suites') or [])
    descend(data.get('suites') or [])
    return cases


def analyze_failures(requirements: Path, run_summary: dict, raw_log: str, output: Path) -> None:
    from mini6_dify_client_streaming import _read_env_file, call_dify
    _read_env_file(BASE / '.env')
    key = os.getenv('DIFY_API_KEY', '').strip()
    if not key:
        print('没有 DIFY_API_KEY，保留本地报告；跳过 Dify 失败分析。', flush=True)
        return
    source = requirements / 'requirements.yaml' if requirements.is_dir() else requirements
    if not source.is_file():
        print(f'需求文件不存在：{source}；跳过 Dify 分析。', flush=True)
        return
    yaml_text = source.read_text(encoding='utf-8-sig')
    if len(yaml_text) > 60000:
        print('需求文件超过 60000 字符；避免截断需求，跳过 Dify 分析。', flush=True)
        return
    evidence = json.dumps(run_summary, ensure_ascii=False, indent=2)
    test_result = 'PLAYWRIGHT EXECUTION SUMMARY:\n' + evidence[:20000] + '\n\nEXECUTION LOG (truncated to last 20000 chars):\n' + raw_log[-20000:]
    base = os.getenv('DIFY_API_BASE_URL', 'https://api.dify.ai/v1')
    print('发送真实测试日志至 Dify analyze ...', flush=True)
    try:
        report = call_dify({
            'inputs': {'mode': 'analyze', 'requirements': yaml_text, 'code_context': '',
                       'visual_context': '', 'test_result': test_result},
            'response_mode': 'streaming', 'user': 'mini6-real-playwright-analysis',
        }, key, base)
        (output / 'dify_analysis.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Dify 分析状态：', report.get('overall_status', 'unknown'), flush=True)
    except Exception as exc:
        print(f'Dify 分析调用未完成：{exc}（不影响原始测试报告）', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', type=Path, default=Path('dify_testing_output_batch/tests'),
                        help='One Dify-generated .spec.ts or a directory of them')
    parser.add_argument('--url', default='http://127.0.0.1:3000',
                        help='Origin of a running LOCAL generated app')
    parser.add_argument('--out', type=Path, default=Path('dify_test_runs'))
    parser.add_argument('--run', action='store_true', help='Explicitly execute generated TypeScript')
    parser.add_argument('--analyze', action='store_true', help='Send real failure logs to Dify')
    parser.add_argument('--requirements', type=Path, help='Required with --analyze')
    parser.add_argument('--skip-browser-install', action='store_true',
                        help='Only if Playwright Chromium is already installed')
    args = parser.parse_args()
    try:
        specs = find_specs(args.spec)
        warnings = inspect_specs(specs)
        print(f'检测到 {len(specs)} 个动态生成的测试文件：', flush=True)
        for item in specs:
            print('  ', item, flush=True)
        for warning in warnings:
            print('检查提示：', warning, flush=True)
        if any('拒绝执行' in m for m in warnings) or any('没找到' in m for m in warnings):
            raise RuntimeError('测试文件未通过基础静态检查；请先修复生成结果')
        if not args.run:
            print('仅检查完成。要真正执行请加 --run。')
            return 0
        if args.analyze and args.requirements is None:
            parser.error('--analyze needs --requirements')
        url = confirm_local_url(args.url)
        check_server(url)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        out = args.out / stamp
        runtime = create_runtime(out, specs, url)
        node = shutil.which('npm')
        npx = shutil.which('npx')
        if not node or not npx:
            raise RuntimeError('npm/npx 未找到；请先安装 Node.js 22.12+ 并在终端确认 npm --version')
        clean = safe_env(url)
        # Dependency installation is a trusted setup step; running AI-generated
        # specs below gets a separate environment WITHOUT API credentials.
        setup_env = os.environ.copy()
        if run_command([node, 'install', '--ignore-scripts', '--no-audit', '--no-fund'], runtime,
                       setup_env, 300, out / 'npm-install.log'):
            raise RuntimeError('安装 @playwright/test 失败：查看 npm-install.log')
        if not args.skip_browser_install:
            if run_command([npx, 'playwright', 'install', 'chromium'], runtime,
                           setup_env, 300, out / 'browser-install.log'):
                raise RuntimeError('安装 Chromium 失败：查看 browser-install.log')
        code = run_command([npx, 'playwright', 'test', '--config=playwright.config.cjs'],
                           runtime, clean, 300, out / 'playwright.log')
        report_path = runtime / 'playwright-report.json'
        data = json.loads(report_path.read_text(encoding='utf-8')) if report_path.is_file() else {}
        stats = data.get('stats') or {}
        tests = collect_tests(data)
        expected = int(stats.get('expected', 0))
        unexpected = int(stats.get('unexpected', 0))
        flaky = int(stats.get('flaky', 0))
        skipped = int(stats.get('skipped', 0))
        if code != 0 or unexpected or flaky:
            status = 'FAILED' if report_path.is_file() and tests else 'ERROR'
        elif skipped or not expected:
            status = 'INCOMPLETE'
        else:
            status = 'PASSED'
        summary = {
            'status': status, 'exit_code': code, 'counts': {'passed': expected,
            'failed': unexpected, 'flaky': flaky, 'skipped': skipped},
            'tests': tests, 'url': url, 'files': [str(x) for x in specs],
            'playwright_log': str((out / 'playwright.log').resolve()),
            'playwright_report': str(report_path.resolve()) if report_path.is_file() else None,
        }
        (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'\n测试结果：{status} | pass={expected} fail={unexpected} skip={skipped} flaky={flaky}', flush=True)
        print('报告目录：', out.resolve(), flush=True)
        if args.analyze and status != 'PASSED':
            analyze_failures(args.requirements, summary,
                             (out / 'playwright.log').read_text(encoding='utf-8', errors='replace'), out)
        return 0 if status == 'PASSED' else 1
    except Exception as exc:
        print('ERROR:', exc, file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
