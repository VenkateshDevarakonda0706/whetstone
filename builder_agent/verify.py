from __future__ import annotations

import json

from builder_agent import config
from builder_agent.llm import ask, extract_json, strip_fences
from builder_agent.sandbox import run_code
from builder_agent.schemas import SubTask, Verdict

_TEST_SYSTEM = (
    "You are a test engineer. Given acceptance criteria and code, "
    "write pytest-style tests that verify each criterion. "
    "Output ONLY executable Python test code, no markdown fencing."
)

_TEST_PROMPT = (
    "Acceptance criteria:\n{criteria}\n\n"
    "Code under test:\n{code}\n\n"
    "Write tests that import nothing external — inline the code if needed."
)

_JUDGE_SYSTEM = (
    "You are a code judge. Score the code 0-10 against the criteria rubric. "
    "Respond with ONLY a JSON object: "
    '{"score": <int>, "issues": [<str>, ...]}'
)

_JUDGE_PROMPT = (
    "Acceptance criteria:\n{criteria}\n\n"
    "Code:\n{code}\n\n"
    "Execution output:\n{exec_output}\n\n"
    "Score 0-10. List concrete issues."
)


def make_tests(subtask: SubTask, code: str) -> str:
    criteria = "\n".join(f"- {c}" for c in subtask.acceptance_criteria)
    prompt = _TEST_PROMPT.format(criteria=criteria, code=code)
    return ask(prompt, model=config.WORKER_MODEL, system=_TEST_SYSTEM)


def verify(subtask: SubTask, code: str | dict[str, str]) -> Verdict:
    if isinstance(code, dict):
        # Format package files for prompt context
        code_str_for_prompt = ""
        for path, content in code.items():
            code_str_for_prompt += f"# File: {path}\n{content}\n\n"

        test_code = strip_fences(make_tests(subtask, code_str_for_prompt))

        # Build a self-extracting, isolated test execution script
        full_code = f"""import os
import sys
import tempfile
import shutil

test_dir = tempfile.mkdtemp()
orig_dir = os.getcwd()
os.chdir(test_dir)
sys.path.insert(0, test_dir)

try:
    files = {repr(code)}
    for path, content in files.items():
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    with open("test_code.py", "w", encoding="utf-8") as f:
        f.write({repr(test_code)})

    try:
        import pytest
        ret = pytest.main(["test_code.py", "-v"])
        sys.exit(ret)
    except ImportError:
        import importlib.util
        spec = importlib.util.spec_from_file_location("test_code", "test_code.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        failed = False
        for name in dir(module):
            if name.startswith("test_") and callable(getattr(module, name)):
                try:
                    getattr(module, name)()
                except AssertionError as e:
                    print(f"Test {{name}} failed: {{e}}", file=sys.stderr)
                    failed = True
                except Exception as e:
                    print(f"Test {{name}} errored: {{e}}", file=sys.stderr)
                    failed = True
        sys.exit(1 if failed else 0)
finally:
    os.chdir(orig_dir)
    try:
        shutil.rmtree(test_dir)
    except Exception:
        pass
"""
    else:
        test_code = strip_fences(make_tests(subtask, code))
        full_code = code + "\n\n" + test_code

    tests_passed, exec_output = run_code(
        full_code, timeout=config.EXEC_TIMEOUT
    )

    if not tests_passed:
        return Verdict(
            passed=False,
            score=0,
            tests_passed=False,
            issues=[f"Tests failed: {exec_output}"],
            exec_output=exec_output,
        )

    criteria = "\n".join(f"- {c}" for c in subtask.acceptance_criteria)
    # Format code string for judge prompt context
    if isinstance(code, dict):
        code_str_for_judge = ""
        for path, content in code.items():
            code_str_for_judge += f"# File: {path}\n{content}\n\n"
    else:
        code_str_for_judge = code

    prompt = _JUDGE_PROMPT.format(
        criteria=criteria, code=code_str_for_judge, exec_output=exec_output
    )
    raw = ask(prompt, model=config.JUDGE_MODEL, system=_JUDGE_SYSTEM)
    data = json.loads(extract_json(raw))
    score = int(data["score"])
    issues = data.get("issues", [])

    return Verdict(
        passed=tests_passed and score >= config.SCORE_THRESHOLD,
        score=score,
        tests_passed=True,
        issues=issues,
        exec_output=exec_output,
    )
