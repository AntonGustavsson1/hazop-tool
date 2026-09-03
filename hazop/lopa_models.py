"""Pure LOPA configuration and calculation helpers.

The GUI and SQLite layer deliberately use this module instead of each
reimplementing the LOPA arithmetic.  It has no Qt or database dependency so
the formulas can be tested deterministically and a locked LOPA revision can
keep using its captured risk-matrix settings.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any


DEFAULT_SAFEGUARD_TYPES = [
    'BPCS', 'SIS', 'Mekanisk', 'Administrativ', 'Övrigt',
]

DEFAULT_ESCALATION_FACTORS = [
    {
        'key': 'antandning',
        'label': 'Antändning / farlig atmosfär',
        'default_percent': 100.0,
    },
    {
        'key': 'narvaro',
        'label': 'Närvaro',
        'default_percent': 100.0,
    },
    {
        'key': 'skada',
        'label': 'Sannolikhet att skadas',
        'default_percent': 100.0,
    },
]

# Inclusive upper limits.  The project may replace these in the LOPA section
# of Riskmatrisinställningar; the defaults mirror the agreed SIL boundaries.
DEFAULT_SIL_BANDS = [
    {'max_rrf': 10.0, 'label': 'SIL 0 / A'},
    {'max_rrf': 100.0, 'label': 'SIL 1'},
    {'max_rrf': 1_000.0, 'label': 'SIL 2'},
    {'max_rrf': 10_000.0, 'label': 'SIL 3'},
    {'max_rrf': 100_000.0, 'label': 'SIL 4'},
]


def _key(value: Any, fallback: str) -> str:
    """Make a stable, readable key from an editable category/factor name."""
    text = str(value or fallback).strip().casefold()
    text = re.sub(r'[^a-z0-9åäö]+', '-', text).strip('-')
    return text or fallback


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def default_tel(level: int) -> float:
    """Conservative editable initial TEL for a one-based severity level."""
    return 10.0 ** (-(max(1, int(level)) + 1))


def _normalise_factor_list(raw: Any) -> list[dict[str, Any]]:
    """Normalise editable escalation-factor definitions without duplicates."""
    values = raw if isinstance(raw, list) else DEFAULT_ESCALATION_FACTORS
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        item = item if isinstance(item, dict) else {'label': item}
        key = _key(item.get('key') or item.get('label'), f'faktor-{index + 1}')
        if key in seen:
            key = f'{key}-{index + 1}'
        seen.add(key)
        percent = _number(item.get('default_percent'), 100.0)
        result.append({
            'key': key,
            'label': str(item.get('label') or key),
            'default_percent': max(0.0, percent if percent is not None else 100.0),
        })
    return result or copy.deepcopy(DEFAULT_ESCALATION_FACTORS)


def normalise_lopa_config(config: Any, matrix: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a complete LOPA configuration tied to a risk-matrix template.

    ``matrix`` supplies the live category keys and number of consequence
    levels.  Existing explicit ``None`` TEL values are retained: blank TEL
    means "not defined", rather than silently inventing a requirement.
    """
    raw = copy.deepcopy(config) if isinstance(config, dict) else {}
    matrix = matrix if isinstance(matrix, dict) else {}
    rows = max(1, int(matrix.get('rows') or 5))
    categories = matrix.get('consequence_categories')
    categories = categories if isinstance(categories, list) else []
    settings_raw = raw.get('category_settings')
    settings_raw = settings_raw if isinstance(settings_raw, dict) else {}

    category_settings: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for index, category in enumerate(categories):
        category = category if isinstance(category, dict) else {}
        category_key = _key(category.get('key') or category.get('name'),
                            f'kategori-{index + 1}')
        if category_key in seen:
            category_key = f'{category_key}-{index + 1}'
        seen.add(category_key)
        source = settings_raw.get(category_key)
        source = source if isinstance(source, dict) else {}
        tel_raw = source.get('tel')
        tel_raw = tel_raw if isinstance(tel_raw, list) else []
        tel: list[float | None] = []
        for level in range(1, rows + 1):
            explicit = tel_raw[level - 1] if level <= len(tel_raw) else '__missing__'
            if explicit is None:
                tel.append(None)
            elif explicit == '__missing__':
                tel.append(default_tel(level))
            else:
                numeric = _number(explicit)
                tel.append(numeric if numeric is not None and numeric > 0 else None)
        category_settings[category_key] = {
            'name': str(category.get('name') or category_key),
            'color': str(category.get('color') or '#64748b'),
            'tel': tel,
            'escalation_factors': _normalise_factor_list(
                source.get('escalation_factors')),
        }

    # Keep settings for a removed category.  They are not used by the live
    # template but remain available in a deliberately retained config/snapshot.
    for old_key, source in settings_raw.items():
        if old_key in category_settings or not isinstance(source, dict):
            continue
        category_settings[str(old_key)] = {
            'name': str(source.get('name') or old_key),
            'color': str(source.get('color') or '#64748b'),
            'tel': list(source.get('tel') or []),
            'escalation_factors': _normalise_factor_list(
                source.get('escalation_factors')),
            'orphaned': True,
        }

    raw_types = raw.get('safeguard_types')
    types: list[str] = []
    seen_types: set[str] = set()
    for item in raw_types if isinstance(raw_types, list) else DEFAULT_SAFEGUARD_TYPES:
        label = str(item or '').strip()
        folded = label.casefold()
        if label and folded not in seen_types:
            types.append(label)
            seen_types.add(folded)
    if not types:
        types = list(DEFAULT_SAFEGUARD_TYPES)

    raw_bands = raw.get('sil_bands')
    bands: list[dict[str, Any]] = []
    for index, item in enumerate(raw_bands if isinstance(raw_bands, list)
                                 else DEFAULT_SIL_BANDS):
        item = item if isinstance(item, dict) else {}
        max_rrf = _number(item.get('max_rrf'))
        if max_rrf is None or max_rrf <= 0:
            continue
        bands.append({
            'max_rrf': max_rrf,
            'label': str(item.get('label') or f'SIL {index}'),
        })
    bands.sort(key=lambda band: band['max_rrf'])
    if not bands:
        bands = copy.deepcopy(DEFAULT_SIL_BANDS)

    assumption = _number(raw.get('default_assumption_percent'), 100.0)
    return {
        'version': 1,
        'default_assumption_percent': max(0.0, assumption if assumption is not None else 100.0),
        'category_settings': category_settings,
        'safeguard_types': types,
        'sil_bands': bands,
    }


def sil_band_for_rrf(rrf: float | None, bands: list[dict[str, Any]] | None = None) -> str | None:
    """Return the first inclusive SIL band covering a required RRF."""
    numeric = _number(rrf)
    if numeric is None or numeric < 0:
        return None
    ordered = normalise_lopa_config({'sil_bands': bands or DEFAULT_SIL_BANDS}).get('sil_bands', [])
    for band in ordered:
        if numeric <= band['max_rrf']:
            return band['label']
    return f"> {ordered[-1]['label']}" if ordered else None


def _barrier_applies(barrier: dict[str, Any], category_key: str) -> bool:
    applies = barrier.get('categories')
    if applies is None:
        return True
    if isinstance(applies, dict):
        return bool(applies.get(category_key, False))
    if isinstance(applies, (list, tuple, set)):
        return category_key in applies
    return bool(applies)


def calculate_lopa(source: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Calculate one LOPA source scenario, returning traceable intermediate data.

    Inputs intentionally stay plain dictionaries so the database snapshots and
    later Excel export can call the same calculation.  A percentage is a
    fraction of the remaining frequency: 10 % is 1/10, not a divisor of 10.
    Missing source frequency or TEL produces an explicit incomplete result
    instead of an unjustified SIL.
    """
    normalised = normalise_lopa_config(config, source.get('matrix'))
    messages: list[str] = []
    frequency = _number(source.get('base_frequency'))
    if frequency is None or frequency < 0:
        messages.append('Numerisk grundfrekvens saknas.')
    assumption = _number(
        source.get('assumption_percent'),
        normalised['default_assumption_percent'])
    if assumption is None or assumption < 0:
        messages.append('Förutsättning måste vara ett procenttal större än eller lika med 0.')
        assumption = 0.0
    effective_frequency = (frequency * assumption / 100.0
                           if frequency is not None and frequency >= 0 else None)

    barriers = [item for item in source.get('barriers', [])
                if isinstance(item, dict) and item.get('active', True)]
    raw_categories = [item for item in source.get('categories', [])
                      if isinstance(item, dict)]
    rows: list[dict[str, Any]] = []
    for category in raw_categories:
        category_key = str(category.get('category_key') or '').strip()
        category_config = normalised['category_settings'].get(category_key)
        if not category_key or category_config is None:
            continue
        active = bool(category.get('active', True))
        severity = int(_number(category.get('severity'), 0) or 0)
        tel = (category_config['tel'][severity - 1]
               if 0 < severity <= len(category_config['tel']) else None)
        if active and severity <= 0:
            messages.append(
                f"Konsekvensnivå saknas för {category.get('category_name') or category_config['name']}.")
        if active and severity > 0 and tel is None:
            messages.append(
                f"TEL saknas för {category.get('category_name') or category_config['name']} nivå {severity}.")
        applicable_rrf = 1.0
        used_barriers: list[dict[str, Any]] = []
        for barrier in barriers:
            if not _barrier_applies(barrier, category_key):
                continue
            rrf = _number(barrier.get('rrf'))
            if rrf is None or rrf < 1.0:
                messages.append(
                    f"Ogiltig RRF för barriären {barrier.get('description') or 'utan namn'}.")
                continue
            applicable_rrf *= rrf
            used_barriers.append({
                'id': barrier.get('id'),
                'description': barrier.get('description') or '',
                'rrf': rrf,
            })
        factor_values = category.get('factors')
        factor_values = factor_values if isinstance(factor_values, dict) else {}
        factor_product = 1.0
        factors: list[dict[str, Any]] = []
        for definition in category_config['escalation_factors']:
            percent = _number(factor_values.get(definition['key']),
                              definition['default_percent'])
            if percent is None or percent < 0:
                messages.append(
                    f"Ogiltig procentsats för {definition['label']}.")
                percent = 0.0
            fraction = percent / 100.0
            factor_product *= fraction
            factors.append({
                'key': definition['key'],
                'label': definition['label'],
                'percent': percent,
                'fraction': fraction,
            })
        remaining_frequency = (effective_frequency / applicable_rrf
                               if effective_frequency is not None else None)
        accident_frequency = (remaining_frequency * factor_product
                              if remaining_frequency is not None else None)
        required_rrf = (accident_frequency / tel
                        if accident_frequency is not None and tel and tel > 0 else None)
        complete = active and frequency is not None and tel is not None and severity > 0
        rows.append({
            # ``categories`` is a flat calculation input, but each entry is
            # an individual HAZOP consequence/category assessment.  Preserve
            # that identity in the result for the LOPA worksheet and export.
            'lopa_consequence_id': category.get('lopa_consequence_id'),
            'hazop_consequence_id': category.get('hazop_consequence_id'),
            'description': category.get('description') or '',
            'category_key': category_key,
            'category_name': category.get('category_name') or category_config['name'],
            'severity': severity,
            'active': active,
            'tel': tel,
            'barrier_rrf': applicable_rrf,
            'barriers': used_barriers,
            'factors': factors,
            'escalation_factor': factor_product,
            'remaining_frequency': remaining_frequency,
            'accident_frequency': accident_frequency,
            'required_rrf': required_rrf,
            'sil': sil_band_for_rrf(required_rrf, normalised['sil_bands']),
            'complete': complete,
        })

    active_rows = [row for row in rows if row['active']]
    candidates = [row for row in active_rows if row['required_rrf'] is not None]
    governing = max(candidates, key=lambda row: row['required_rrf'], default=None)
    if not active_rows:
        messages.append('Ingen aktiv konsekvenskategori är vald.')
    if active_rows and not candidates:
        messages.append('Ingen aktiv konsekvenskategori har tillräckliga data för RRF/SIL.')
    return {
        'base_frequency': frequency,
        'assumption_percent': assumption,
        'effective_frequency': effective_frequency,
        'categories': rows,
        'governing_category_key': governing['category_key'] if governing else None,
        'governing_category_name': governing['category_name'] if governing else None,
        'required_rrf': governing['required_rrf'] if governing else None,
        'sil': governing['sil'] if governing else None,
        'complete': bool(governing) and all(row['complete'] for row in active_rows),
        'messages': list(dict.fromkeys(messages)),
    }
