"""Каталог готовых шаблонов (templates/*.json): все файлы проходят тот же валидатор, что импорт
(parse_bot_template), карточки каталога собираются без сетевого доступа. Без Postgres."""
import json
import logging
from pathlib import Path

import pytest

from bothub import main

pytestmark = pytest.mark.pure


def _templates_dir() -> Path:
    """Каталог шаблонов рядом с репо. Тест идёт из core/tests/, репо — parents[2] от __file__."""
    return Path(__file__).resolve().parents[2] / 'templates'


def test_templates_dir_exists_and_not_empty():
    root = _templates_dir()
    assert root.is_dir(), f'каталог {root} не найден'
    files = sorted(root.glob('*.json'))
    assert files, 'в каталоге templates нет *.json'
    assert {'researcher', 'mail_triage', 'pr_reviewer', 'daily_digest', 'house_helper', 'translator'} <= {path.stem for path in files}


def test_all_templates_pass_validator():
    # Главная проверка задачи: каждый файл — валидный документ шаблона бота. Без этой гарантии
    # ядро пропустило бы файл через parse_bot_template при GET /api/templates/catalog/{id} и 404-нуло бы.
    root = _templates_dir()
    seen_ids: set[str] = set()
    for path in sorted(root.glob('*.json')):
        template_id = path.stem
        with path.open('r', encoding='utf-8') as fh:
            doc = json.loads(fh.read())
        assert template_id not in seen_ids, f'дубликат id в каталоге: {template_id}'
        seen_ids.add(template_id)
        try:
            parsed, _ = main.validate_bot_template(doc)
        except main.BotTemplateError as exc:
            pytest.fail(f'{path.name}: validate_bot_template отказал: {exc}')
        # Карточка каталога строится из этих полей; пустое имя или аватар делают карточку бесполезной.
        assert parsed['name'].strip(), f'{path.name}: пустое имя'
        assert parsed['avatar'].strip(), f'{path.name}: пустой avatar'
        # id — имя файла без .json — должен ложиться в BOT_TEMPLATE_CATALOG_ID (тот же класс, что у новых ботов).
        assert main.BOT_TEMPLATE_CATALOG_ID.fullmatch(template_id), \
            f'{path.name}: id {template_id!r} не подходит под BOT_TEMPLATE_CATALOG_ID'


def test_catalog_returns_one_card_per_file(monkeypatch):
    # Карточки идут в порядке id (имя файла); описание берётся из первой непустой строки инструкций.
    root = _templates_dir()
    files = sorted(root.glob('*.json'))
    monkeypatch.setattr(main, '_catalog_templates_dir', lambda: root)
    cards = main._load_catalog_entries()
    assert [c['id'] for c in cards] == [p.stem for p in files]
    for card, path in zip(cards, files):
        with path.open('r', encoding='utf-8') as fh:
            doc = json.loads(fh.read())
        assert card['name'] == doc['name']
        assert card['role'] == doc.get('role', '')
        assert card['avatar'] == doc.get('avatar', 'robot')
        # description: первая непустая строка инструкций; для коротких текстов == первой строке.
        first_line = next((line.strip() for line in doc['instructions'].splitlines() if line.strip()), '')
        expected_desc = first_line[:main.BOT_TEMPLATE_CATALOG_DESC_MAX] if first_line else doc.get('role', '')
        assert card['description'] == expected_desc


def test_catalog_skips_invalid_file_with_warning(monkeypatch, caplog):
    # Битый файл не должен валить весь каталог: один такой — и ядро логирует warning и идёт дальше.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        # Один валидный файл, один битый: валидный должен остаться в карточках.
        (tmp_root / 'good.json').write_text(
            json.dumps({'format': 'botstead-bot', 'version': 1, 'name': 'G', 'instructions': 'hi'}),
            encoding='utf-8')
        (tmp_root / 'bad.json').write_text('{"name": "x", "format": "other/1"}', encoding='utf-8')
        monkeypatch.setattr(main, '_catalog_templates_dir', lambda: tmp_root)
        with caplog.at_level(logging.WARNING, logger='bothub'):
            cards = main._load_catalog_entries()
        assert [c['id'] for c in cards] == ['good']
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        # record.msg — формат-строка ('catalog_template_invalid'), имя файла — в record.file (extra=).
        matching = [r for r in warnings if r.msg == 'catalog_template_invalid' and getattr(r, 'file', '') == 'bad.json']
        assert matching, f'нет warning про битый файл: {[(r.msg, getattr(r, "file", "")) for r in warnings]}'


def test_catalog_skips_invalid_procedure_and_outside_symlink(monkeypatch, tmp_path, caplog):
    # Проверка каталога совпадает с импортом: неверная процедура не попадает ни в список, ни в полный ответ.
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({'name': 'Broken', 'procedures': [{'name': 'P', 'steps': []}]}), encoding='utf-8')
    outside = tmp_path.parent / 'outside-catalog-template.json'
    outside.write_text(json.dumps({'name': 'Outside'}), encoding='utf-8')
    link = tmp_path / 'outside.json'
    link.symlink_to(outside)
    monkeypatch.setattr(main, '_catalog_templates_dir', lambda: tmp_path)
    with caplog.at_level(logging.WARNING, logger='bothub'):
        assert main._load_catalog_entries() == []
    assert {getattr(record, 'file', '') for record in caplog.records if record.msg == 'catalog_template_invalid'} == {'bad.json', 'outside.json'}
    with pytest.raises(ValueError):
        main._read_catalog_template(tmp_path, bad)
    with pytest.raises(ValueError):
        main._read_catalog_template(tmp_path, link)


def test_catalog_accepts_importable_procedure(tmp_path):
    doc = {'name': 'With procedure', 'procedures': [
        {'format': 'bothub-procedure/1', 'name': 'Open', 'params': [], 'steps': [
            {'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Next'}, 'risk': 'none'},
        ]},
    ]}
    path = tmp_path / 'with_procedure.json'
    path.write_text(json.dumps(doc), encoding='utf-8')
    original, parsed = main._read_catalog_template(tmp_path, path)
    assert original == doc
    assert parsed['name'] == 'With procedure'
    _, checked = main.validate_bot_template(original)
    assert checked[0][0] == 'Open'


def test_house_helper_has_no_write_tools():
    doc = json.loads((_templates_dir() / 'house_helper.json').read_text(encoding='utf-8'))
    assert {rule['tool'] for rule in doc['auto_allow']} <= {
        'Read', 'mcp__bothub__mac_find_files', 'mcp__bothub__mac_read_file',
        'mcp__bothub__mac_preview', 'mcp__bothub__mac_screenshot',
    }
    assert all(tool.rsplit('__', 1)[-1] in {'get_state', 'list_entities', 'history'} for tool in doc['mcp_allow'])


def test_catalog_description_truncates_long_first_line():
    # Первая строка длиннее предела — режется по словам, не посередине; если слов нет — тупо по длине.
    long = 'word ' * 200  # 1000 символов с пробелами
    trimmed = main._catalog_description(long.strip())
    assert len(trimmed) <= main.BOT_TEMPLATE_CATALOG_DESC_MAX
    assert not trimmed.endswith(' ') and trimmed  # без висячего пробела
    # Короткая строка возвращается как есть
    assert main._catalog_description('short') == 'short'
    # Пустые строки и переводы — пустая строка
    assert main._catalog_description('') == ''
    assert main._catalog_description('\n\n   \n') == ''


def test_catalog_description_fallback_is_bounded(monkeypatch, tmp_path):
    (tmp_path / 'blank.json').write_text(json.dumps({'name': 'Blank', 'role': 'word ' * 100}), encoding='utf-8')
    monkeypatch.setattr(main, '_catalog_templates_dir', lambda: tmp_path)
    cards = main._load_catalog_entries()
    assert len(cards) == 1
    assert 0 < len(cards[0]['description']) <= main.BOT_TEMPLATE_CATALOG_DESC_MAX


def test_catalog_id_pattern_matches_real_filenames():
    # Шаблон id должен принимать все шесть файлов и отвергать обход пути.
    root = _templates_dir()
    for path in root.glob('*.json'):
        assert main.BOT_TEMPLATE_CATALOG_ID.fullmatch(path.stem)
    # Обход пути и недопустимые символы отклоняются.
    assert not main.BOT_TEMPLATE_CATALOG_ID.fullmatch('..')
    assert not main.BOT_TEMPLATE_CATALOG_ID.fullmatch('a/b')
    assert not main.BOT_TEMPLATE_CATALOG_ID.fullmatch('A')  # верхний регистр запрещён
    assert not main.BOT_TEMPLATE_CATALOG_ID.fullmatch('')


def test_catalog_dir_respects_env(monkeypatch):
    # BOTHUB_TEMPLATES_DIR перебивает путь по умолчанию: env важна для Docker-образа ядра.
    root = _templates_dir()
    monkeypatch.setenv('BOTHUB_TEMPLATES_DIR', str(root))
    assert main._catalog_templates_dir() == root
    monkeypatch.delenv('BOTHUB_TEMPLATES_DIR')
    # Без env: если каталог templates/ есть рядом с репо — используется; иначе None.
    if root.is_dir():
        assert main._catalog_templates_dir() == root
