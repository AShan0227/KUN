"""CodeReader — find and explain nearby code with anchor-expand semantics."""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from pathlib import Path

from kun.core.anchor_expand import AnchorExpandIterator

ExplainFn = Callable[[str, str], str | Awaitable[str]]

_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
_TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".pyi",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_MAX_SCAN_BYTES = 1024 * 1024


class CodeReader:
    """Read codebase structure and return small, relevant slices."""

    def __init__(
        self,
        *,
        root: str | Path = ".",
        explainer: ExplainFn | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.explainer = explainer

    async def find_anchor_file(self, query: str, root: str = ".") -> str | None:
        """Find the most relevant file for a query using cheap local scoring."""
        search_root = self._resolve_dir(root)
        terms = _query_terms(query)
        if not terms:
            return None

        best: tuple[int, str] | None = None
        for path in _iter_text_files(search_root):
            score = _score_file(path, terms)
            if score <= 0:
                continue
            rel = _rel(path, self.root)
            candidate = (score, rel)
            if (
                best is None
                or candidate[0] > best[0]
                or (candidate[0] == best[0] and candidate[1] < best[1])
            ):
                best = candidate
        return best[1] if best else None

    async def get_dependencies(self, file_path: str) -> list[str]:
        """Parse Python imports and resolve local files under the reader root."""
        path = self._resolve_file(file_path)
        if path.suffix != ".py":
            return []
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            return []

        deps: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    deps.extend(self._resolve_import(alias.name, current_file=path, level=0))
            elif isinstance(node, ast.ImportFrom):
                deps.extend(
                    self._resolve_import(
                        node.module or "",
                        current_file=path,
                        level=node.level,
                        imported_names=[alias.name for alias in node.names],
                    )
                )
        return _dedupe(deps)

    async def get_callers(self, symbol: str, root: str = ".") -> list[str]:
        """Return file:line snippets where the symbol appears."""
        search_root = self._resolve_dir(root)
        needle = symbol.strip()
        if not needle:
            return []
        pattern = re.compile(rf"\b{re.escape(needle)}\b")
        matches: list[str] = []
        for path in _iter_text_files(search_root):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(lines, start=1):
                if pattern.search(line):
                    matches.append(f"{_rel(path, self.root)}:{lineno}:{line.strip()}")
        return matches

    async def explain(
        self,
        file_path: str,
        lines: tuple[int, int] | None = None,
    ) -> str:
        """Explain a file or line range; uses injected LLM explainer when present."""
        path = self._resolve_file(file_path)
        content = path.read_text(encoding="utf-8")
        selected = _select_lines(content, lines)
        rel = _rel(path, self.root)
        if self.explainer is not None:
            result = self.explainer(rel, selected)
            if inspect.isawaitable(result):
                return await result
            return result
        return _basic_explanation(rel, selected)

    async def read_anchor_then_expand(
        self,
        query: str,
        *,
        root: str = ".",
        max_rounds: int = 3,
    ) -> AsyncIterator[str]:
        """Yield an anchor file first, then local dependency neighbors."""
        anchor = await self.find_anchor_file(query, root=root)
        if anchor is None:
            return
        dependencies = await self.get_dependencies(anchor)

        async def anchor_fn() -> str:
            return anchor

        async def expand_fn(_anchor: str, prior: list[str]) -> str | None:
            used = set(prior)
            return next((dep for dep in dependencies if dep not in used), None)

        async for item in AnchorExpandIterator(
            anchor_fn,
            expand_fn,
            max_rounds=max_rounds,
        ):
            yield item

    def _resolve_dir(self, rel: str) -> Path:
        path = _resolve_under_root(self.root, rel)
        if not path.exists() or not path.is_dir():
            raise ValueError(f"not a directory under root: {rel}")
        return path

    def _resolve_file(self, rel: str) -> Path:
        path = _resolve_under_root(self.root, rel)
        if not path.exists() or not path.is_file():
            raise ValueError(f"not a file under root: {rel}")
        return path

    def _resolve_import(
        self,
        module: str,
        *,
        current_file: Path,
        level: int,
        imported_names: list[str] | None = None,
    ) -> list[str]:
        base = current_file.parent
        if level > 0:
            for _ in range(level - 1):
                base = base.parent
            module_parts = [part for part in module.split(".") if part]
            candidates = _module_candidates(base, module_parts)
        else:
            module_parts = [part for part in module.split(".") if part]
            candidates = _module_candidates(self.root, module_parts)

        for name in imported_names or []:
            if name == "*":
                continue
            for candidate_base in list(candidates):
                if candidate_base.name == "__init__.py":
                    candidates.extend(_module_candidates(candidate_base.parent, [name]))
                elif candidate_base.is_dir():
                    candidates.extend(_module_candidates(candidate_base, [name]))

        resolved: list[str] = []
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                try:
                    resolved.append(_rel(candidate.resolve(), self.root))
                except ValueError:
                    continue
        return resolved


def _resolve_under_root(root: Path, rel: str | Path) -> Path:
    candidate = Path(rel)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError(f"path escapes code root: {rel}")
    return resolved


def _iter_text_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > _MAX_SCAN_BYTES:
                continue
        except OSError:
            continue
        yield path


def _query_terms(query: str) -> list[str]:
    return [term.lower() for term in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query) if len(term) > 1]


def _score_file(path: Path, terms: list[str]) -> int:
    score = 0
    name = path.name.lower()
    rel = str(path).lower()
    for term in terms:
        if term in name:
            score += 30
        if term in rel:
            score += 10
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return score
    for term in terms:
        score += min(text.count(term), 20)
    return score


def _module_candidates(base: Path, module_parts: list[str]) -> list[Path]:
    if not module_parts:
        return []
    module_path = base.joinpath(*module_parts)
    return [module_path.with_suffix(".py"), module_path / "__init__.py"]


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _select_lines(content: str, lines: tuple[int, int] | None) -> str:
    if lines is None:
        return content
    start, end = lines
    split = content.splitlines()
    start_idx = max(0, start - 1)
    end_idx = min(len(split), end)
    return "\n".join(split[start_idx:end_idx])


def _basic_explanation(rel: str, content: str) -> str:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return f"{rel}: Python file with syntax errors or partial code."
    functions = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    classes = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)]
    parts = [f"{rel}: {len(content.splitlines())} lines"]
    if classes:
        parts.append(f"classes={', '.join(classes[:5])}")
    if functions:
        parts.append(f"functions={', '.join(functions[:8])}")
    if imports:
        parts.append(f"imports={len(imports)}")
    return "; ".join(parts)


__all__ = ["CodeReader", "ExplainFn"]
