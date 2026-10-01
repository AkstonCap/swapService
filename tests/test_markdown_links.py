"""Keep link validation focused on functional repository documentation."""
import importlib.util
from pathlib import Path


CHECKER_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'check_markdown_links.py'
spec = importlib.util.spec_from_file_location('markdown_link_checker', CHECKER_PATH)
assert spec is not None and spec.loader is not None
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_informative_root_vision_is_not_a_functional_link_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(checker, 'ROOT', tmp_path)
    (tmp_path / 'vision.md').write_text(
        '[strategy](../../strategy.docx)\n[context](missing-notes.md)\n', encoding='utf-8',
    )
    (tmp_path / 'README.md').write_text('[guide](guide.md)\n', encoding='utf-8')
    (tmp_path / 'guide.md').write_text('# Guide\n', encoding='utf-8')
    assert checker.broken_links() == []


def test_functional_documentation_still_rejects_missing_or_escaping_links(tmp_path, monkeypatch):
    monkeypatch.setattr(checker, 'ROOT', tmp_path)
    (tmp_path / 'README.md').write_text(
        '[missing](missing.md)\n[escape](../outside.md)\n', encoding='utf-8',
    )
    assert checker.broken_links() == [
        'README.md:1: missing local link target: missing.md',
        'README.md:2: link escapes repository: ../outside.md',
    ]


def test_vision_exception_does_not_skip_nested_functional_documents(tmp_path, monkeypatch):
    monkeypatch.setattr(checker, 'ROOT', tmp_path)
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs' / 'vision.md').write_text('[broken](missing.md)\n', encoding='utf-8')
    assert checker.broken_links() == ['docs/vision.md:1: missing local link target: missing.md']
