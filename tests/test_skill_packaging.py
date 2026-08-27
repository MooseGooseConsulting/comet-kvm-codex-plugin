from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import glkvm_mcp


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / ".claude" / "skills"
EXPECTED_SKILLS = {"comet-bios-triage", "comet-kvm-operations"}
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
TOOL_NAME_RE = re.compile(r"`((?:bios|comet|kvm)_[a-z0-9_]+)`")


def _skill_dirs() -> list[Path]:
    return sorted(path.parent for path in SKILLS_ROOT.glob("*/SKILL.md"))


def _frontmatter(path: Path) -> tuple[list[str], str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines and lines[0] == "---", f"{path} must start with YAML frontmatter"
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise AssertionError(f"{path} has unterminated YAML frontmatter") from exc
    return lines[1:end], "\n".join(lines[end + 1 :])


def _quoted_metadata_value(text: str, key: str) -> str:
    match = re.search(rf'^  {re.escape(key)}: "([^"]+)"$', text, flags=re.MULTILINE)
    assert match is not None, f"missing quoted interface.{key}"
    return match.group(1)


def test_expected_skill_packages_exist() -> None:
    assert {path.name for path in _skill_dirs()} == EXPECTED_SKILLS


def test_plugin_and_readme_advertise_both_skill_routes() -> None:
    manifest = json.loads((REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["skills"] == "./.claude/skills/"

    description = " ".join(
        [
            manifest["description"],
            manifest["interface"]["longDescription"],
            *manifest["interface"]["defaultPrompt"],
        ]
    ).lower()
    for required in ("operations", "recovery", "virtual media", "bios"):
        assert required in description

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for skill_name in EXPECTED_SKILLS:
        assert f"{skill_name}/" in readme


def test_plugin_manifest_version_matches_authoritative_package_version() -> None:
    """Publishing must not install metadata for a stale plugin release."""
    manifest = json.loads((REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    package = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', package, flags=re.MULTILINE)
    assert match is not None, "pyproject.toml must declare a project version"

    assert manifest["version"] == match.group(1)


def test_skill_frontmatter_is_minimal_and_matches_directory() -> None:
    for skill_dir in _skill_dirs():
        frontmatter, body = _frontmatter(skill_dir / "SKILL.md")
        keys = [
            match.group(1)
            for line in frontmatter
            if (match := re.match(r"^([a-z][a-z0-9_-]*):", line))
        ]
        assert keys == ["name", "description"], (skill_dir, keys)

        name_line = next(line for line in frontmatter if line.startswith("name:"))
        assert name_line.partition(":")[2].strip() == skill_dir.name

        description_index = next(
            index for index, line in enumerate(frontmatter) if line.startswith("description:")
        )
        description_lines = frontmatter[description_index:]
        assert any(line.strip() not in {"description:", "description: >"} for line in description_lines)
        assert body.strip(), f"{skill_dir / 'SKILL.md'} must contain instructions"


def test_all_local_markdown_references_exist_inside_the_skill_payload() -> None:
    for skill_dir in _skill_dirs():
        markdown_files = [skill_dir / "SKILL.md", *sorted((skill_dir / "references").glob("*.md"))]
        for markdown_file in markdown_files:
            for raw_target in MARKDOWN_LINK_RE.findall(markdown_file.read_text(encoding="utf-8")):
                target = raw_target.split("#", 1)[0]
                if not target or target.startswith(("https://", "http://", "mailto:")):
                    continue
                resolved = (markdown_file.parent / target).resolve()
                try:
                    resolved.relative_to(skill_dir.resolve())
                except ValueError as exc:
                    raise AssertionError(
                        f"{markdown_file} references content outside its skill payload: {raw_target}"
                    ) from exc
                assert resolved.is_file(), f"missing skill reference: {markdown_file} -> {raw_target}"


def test_packaged_skills_do_not_depend_on_removed_or_external_runtime_surfaces() -> None:
    forbidden = {
        "../../docs/": "repo docs are not part of the skill payload",
        "approval_id": "approval-token workflow was removed",
        "src/bios_sidecar/policy/": "no executable policy package exists",
        "D:\\_projects\\hwinfo-cpu-triage": "workstation-specific project is not bundled",
        "automatically prefers the Comet/PiKVM native OCR": "MCP OCR uses host Tesseract",
    }
    for skill_dir in _skill_dirs():
        for path in skill_dir.rglob("*"):
            if not path.is_file() or path.suffix not in {".md", ".json", ".yaml"}:
                continue
            text = path.read_text(encoding="utf-8")
            for needle, reason in forbidden.items():
                assert needle not in text, f"{path} contains {needle!r}: {reason}"


def test_each_skill_has_valid_openai_interface_metadata() -> None:
    for skill_dir in _skill_dirs():
        metadata_path = skill_dir / "agents" / "openai.yaml"
        assert metadata_path.is_file()
        text = metadata_path.read_text(encoding="utf-8")
        assert re.findall(r"^([a-z][a-z0-9_]*):$", text, flags=re.MULTILINE) == [
            "interface",
            "policy",
        ]

        display_name = _quoted_metadata_value(text, "display_name")
        short_description = _quoted_metadata_value(text, "short_description")
        default_prompt = _quoted_metadata_value(text, "default_prompt")
        assert display_name
        assert 25 <= len(short_description) <= 64
        assert f"${skill_dir.name}" in default_prompt
        assert re.search(r"^  allow_implicit_invocation: true$", text, flags=re.MULTILINE)


def test_trigger_eval_files_have_balanced_deterministic_schema() -> None:
    for skill_dir in _skill_dirs():
        eval_path = skill_dir / "evals" / "trigger-cases.json"
        data = json.loads(eval_path.read_text(encoding="utf-8"))
        assert data["skill"] == skill_dir.name
        assert isinstance(data["purpose"], str) and data["purpose"].strip()
        assert isinstance(data["cases"], list) and len(data["cases"]) >= 10

        queries: set[str] = set()
        outcomes: set[bool] = set()
        for case in data["cases"]:
            assert set(case) == {"query", "should_trigger"}
            assert isinstance(case["query"], str) and case["query"].strip()
            assert isinstance(case["should_trigger"], bool)
            assert case["query"] not in queries
            queries.add(case["query"])
            outcomes.add(case["should_trigger"])
        assert outcomes == {False, True}


def test_named_skill_tools_exist_on_the_registered_mcp_surface() -> None:
    registered = {tool.name for tool in asyncio.run(glkvm_mcp.mcp.list_tools())}
    mentioned: set[str] = set()

    for skill_dir in _skill_dirs():
        for path in [skill_dir / "SKILL.md", *sorted((skill_dir / "references").glob("*.md"))]:
            mentioned.update(TOOL_NAME_RE.findall(path.read_text(encoding="utf-8")))

    assert mentioned, "expected skills to name MCP tools"
    assert mentioned <= registered, f"unknown skill tools: {sorted(mentioned - registered)}"
