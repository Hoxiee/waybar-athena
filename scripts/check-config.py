#!/usr/bin/env python3
"""Validate the distributable Waybar config without starting the bar."""

import argparse
import ast
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JSONC_TOKENS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/')


def read_jsonc(path):
    text = JSONC_TOKENS.sub(
        lambda match: match[0] if match[0].startswith('"') else " ", path.read_text())
    # Ignore trailing commas, without rewriting punctuation inside strings.
    text = re.sub(r'"(?:\\.|[^"\\])*"|,\s*(?=[}\]])',
                  lambda match: match[0] if match[0].startswith('"') else "", text)
    return json.loads(text)


def check():
    configs = {path: read_jsonc(path) for path in [ROOT / "config.jsonc", *sorted((ROOT / "modules").glob("*.jsonc"))]}
    config = configs[ROOT / "config.jsonc"]
    included = {(ROOT / name).resolve() for name in config["include"]}
    expected = {path.resolve() for path in configs if path.parent.name == "modules"}
    assert included == expected, "include list must contain every module config exactly once"
    assert len(included) == len(config["include"]), "duplicate config include"
    modules = {}
    for path in sorted(expected):
        for name, options in configs[path].items():
            assert name not in modules, f"duplicate module: {name}"
            modules[name] = options

    def visit(name, ancestors=()):
        assert name in modules, f"undefined module: {name}"
        assert name not in ancestors, f"circular group: {name}"
        if name.startswith("group/"):
            for child in modules[name]["modules"]:
                visit(child, (*ancestors, name))

    for position in ("left", "center", "right"):
        for name in config[f"modules-{position}"]:
            visit(name)

    for name, options in modules.items():
        for key, command in options.items():
            if (key == "exec" or key.startswith("on-")) and isinstance(command, str):
                for word in shlex.split(command):
                    if word.startswith("~/.config/waybar/"):
                        path = ROOT / word.removeprefix("~/.config/waybar/")
                        assert path.is_file() and not path.is_symlink(), f"missing or external script: {name}: {word}"
                        assert os.access(path, os.X_OK), f"script must be executable: {word}"

    assert "interval" not in modules["custom/claude"], "Claude monitor must stream"
    assert modules["custom/claude"].get("exec-on-event") is False, "clicks must not restart the Claude monitor"

    seen = set()

    def stylesheet(path):
        assert path not in seen, f"duplicate/circular CSS import: {path}"
        seen.add(path)
        text = path.read_text()
        for relative in re.findall(r'@import\s+"([^"]+)"\s*;', text):
            stylesheet((path.parent / relative).resolve())
        return re.sub(r'/\*[\s\S]*?\*/', '', text)

    # Walk imports first, then inspect only the stylesheets actually loaded.
    stylesheet(ROOT / "style.css")
    assert {p.resolve() for p in (ROOT / "tokens").glob("*.css")} <= seen, "unloaded CSS token file"
    css = "\n".join(re.sub(r'/\*[\s\S]*?\*/', '', path.read_text()) for path in seen)
    defined = set(re.findall(r'@define-color\s+(\w+)', css))
    used = set(re.findall(r'@(\w+)', css)) - {"import", "define", "keyframes"}
    assert used <= defined, f"undefined CSS colours: {sorted(used - defined)}"

    for path in (ROOT / "scripts").glob("*.py"):
        ast.parse(path.read_text(), filename=str(path))
    for path in (ROOT / "scripts").glob("*.sh"):
        if not path.name.endswith(".local.sh"):
            subprocess.run(["bash", "-n", str(path)], check=True)
    print(f"Validated {len(configs)} JSONC files, {len(modules)} modules, CSS tokens, and script syntax")


def check_gtk():
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    assert Gtk.init_check()[0], "GTK check needs a graphical session or xvfb-run"
    provider = Gtk.CssProvider()
    issues = []
    provider.connect("parsing-error", lambda _, section, error: issues.append(str(error)))
    provider.load_from_path(str(ROOT / "style.css"))
    assert not issues, "\n".join(issues)
    print("GTK3 parsed the complete stylesheet without errors")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gtk", action="store_true", help="also parse CSS with GTK3 (requires a display)")
    args = parser.parse_args()
    check()
    if args.gtk:
        check_gtk()
