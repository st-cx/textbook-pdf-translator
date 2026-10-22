"""Read-only native glyph presence audit of saved protected source assets.

This compares source ledger characters/fonts/sizes with the actual isolated
PDF objects, before translation or output composition. It cannot establish a
formula's mathematical meaning, reading order or complete visible geometry.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path
import re
import time

import pymupdf as fitz

from .models import (Book, TOKEN_RE, RULES_VERSION, digest, file_sha256,
                     protected_asset_descriptor, source_contract_issues)


def _signature(char, font, size):
    return (char, re.sub(r'[^a-z0-9]', '', font.split('+')[-1][:31].lower()), round(float(size), 2))


def _native_counts(page):
    """Use the ledger's rawdict metric, including anisotropic PDF text matrices.

    get_texttrace reports a different size for horizontally stretched text.
    Comparing that with the ledger's rawdict size falsely reports intact glyphs
    as missing. No font/size tolerance is added here. Infinite extraction bounds
    avoid silently filtering text by nominal font boxes; visible clipping still
    requires the separate source/output pixel checks.
    """
    raw = page.get_text('rawdict', clip=fitz.INFINITE_RECT(),
                        flags=fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_PRESERVE_WHITESPACE)
    return Counter(_signature(char['c'], span['font'], span['size'])
                   for block in raw['blocks'] if 'lines' in block
                   for line in block['lines'] for span in line['spans']
                   for char in span['chars'] if char['c'].strip())


def _owners(book):
    owners = defaultdict(set)
    for element in book.elements:
        if not element.metadata.get('render_suppressed'):
            for aid in set(element.asset_ids) | set(TOKEN_RE.findall(element.text)):
                owners[aid].add(element.id)
    return owners


def _resolve_file(value, roots):
    path = Path(value)
    if not path.is_absolute():
        path = next((root/path for root in roots if (root/path).is_file()), roots[0]/path)
    return path.resolve()


def audit_binding(book, job_dir):
    """Read-only identity for a native asset audit and safe resume checks.

    Bind real file contents, not only claimed bundle hashes. Missing/unreadable
    evidence remains an explicit value so an unavailable ledger cannot make the
    entire binding disappear. Only the signature is needed for equality checks.
    """
    roots = (Path(book.profile.get('job_dir', job_dir)).resolve(), Path(job_dir).resolve())
    owners = _owners(book)
    descriptors, asset_files, ledgers, cached_files = {}, {}, {}, {}

    def fingerprint(value):
        if not value:
            return {'available': False, 'reason': 'missing_path'}
        path = _resolve_file(value, roots)
        if path not in cached_files:
            try:
                cached_files[path] = {'available': True, 'sha256': file_sha256(path)}
            except (OSError, ValueError) as error:
                cached_files[path] = {'available': False, 'reason': type(error).__name__}
        return cached_files[path]

    for aid in sorted(owners):
        asset = book.assets.get(aid)
        if asset is None:
            descriptors[aid] = {'missing': True, 'owner_ids': sorted(owners[aid])}
            continue
        descriptors[aid] = {'descriptor': protected_asset_descriptor(asset),
                            'glyph_ids': asset.metadata.get('glyph_ids'),
                            'owner_ids': sorted(owners[aid])}
        asset_files[asset.path] = fingerprint(asset.path)
    for page in book.pages:
        coverage = page.get('coverage', {})
        ledgers[str(page['number'])] = {'basis': coverage.get('basis'),
                                        'ledger': fingerprint(coverage.get('ledger'))}
    contract = {'schema_version': book.schema_version, 'rules_version': RULES_VERSION,
                'source_sha256': book.source_sha256, 'selected_pages': book.selected_pages,
                'elements': [(e.id, e.source_hash, e.page, e.metadata.get('render_suppressed', False))
                             for e in book.elements],
                'source_contract_issues': source_contract_issues(book)}
    result = {'schema': 'native-glyph-audit-binding-v1',
              'audit_source_sha256': file_sha256(__file__),
              'source_contract_sha256': digest(contract),
              'owned_assets_sha256': digest(descriptors),
              'coverage_ledgers_sha256': digest(ledgers),
              'asset_files_sha256': digest(asset_files)}
    result['signature'] = digest(result)
    return result


def audit_native_assets(book, job_dir, output_dir):
    started = time.perf_counter()
    job_dir, output_dir = Path(job_dir).resolve(), Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_root = Path(book.profile.get('job_dir', job_dir))
    binding_start = audit_binding(book, job_dir)
    owners = _owners(book)
    pages = {p['number']: p for p in book.pages}
    by_page = defaultdict(list)
    for aid in owners:
        if aid in book.assets and book.assets[aid].metadata.get('glyph_ids'):
            by_page[book.assets[aid].page].append(book.assets[aid])
    report = {'schema_version': 2, 'source_sha256': book.source_sha256, 'read_only': True,
              'audit_source_sha256': file_sha256(__file__),
              'comparison_basis': 'rawdict-character-font-size-counter-v2',
              'eligible_assets': 0, 'checked_assets': 0, 'passed_assets': 0,
              'missing_glyph_assets': 0, 'unverifiable_assets': 0, 'selected_source_glyphs': 0,
              'extra_extracted_glyphs_not_certified': 0, 'issues': [], 'page_results': [],
              'limitations': ['Native character/font/size counts establish presence only; position and mathematical meaning are not certified.',
                              'Original scan images and native objects without glyph identities require separate raster/vector checks.',
                              'Extra extracted text can be outside a PDF clip; this audit does not certify it as visible contamination.']}
    hashes = {}
    current_doc, current_path = None, None
    try:
        for page, native in sorted(by_page.items()):
            coverage = pages.get(page, {}).get('coverage', {})
            if coverage.get('basis') == 'raster-ink-and-ocr-units':
                continue
            # Count before opening evidence, including whole-page ledger failure.
            report['eligible_assets'] += len(native)
            page_result = {'page': page, 'eligible': len(native), 'checked': 0,
                           'passed': 0, 'missing': 0, 'unverifiable': 0}
            report['page_results'].append(page_result)
            roots = (source_root, job_dir)
            ledger = next((root/coverage.get('ledger', '') for root in roots
                           if coverage.get('ledger') and (root/coverage['ledger']).is_file()), None)
            if ledger is None:
                report['unverifiable_assets'] += len(native)
                page_result['unverifiable'] += len(native)
                report['issues'].append({'code': 'source_asset_ledger_unavailable', 'severity': 'error', 'page': page,
                                         'asset_ids': [a.id for a in native],
                                         'detail': 'Protected source glyph identities cannot be checked without their original ledger'})
                continue
            try:
                with gzip.open(ledger, 'rt', encoding='utf8') as stream:
                    records = json.load(stream)
                glyphs = {g['id']: g for g in records}
                if len(glyphs) != len(records):
                    raise ValueError('Native ledger contains duplicate glyph identities')
            except (KeyError, ValueError, OSError, TypeError, EOFError) as error:
                report['unverifiable_assets'] += len(native)
                page_result['unverifiable'] += len(native)
                report['issues'].append({'code': 'source_asset_ledger_unavailable', 'severity': 'error', 'page': page,
                                         'asset_ids': [a.id for a in native], 'error_type': type(error).__name__,
                                         'reason': str(error),
                                         'detail': 'Protected source glyph identities cannot be checked against an invalid native ledger'})
                continue
            for asset in native:
                try:
                    selected = [glyphs[gid] for gid in asset.metadata['glyph_ids']]
                    expected = Counter(_signature(g['c'], g['font'], g['size']) for g in selected if g['c'].strip())
                    if not expected:
                        report['unverifiable_assets'] += 1
                        page_result['unverifiable'] += 1
                        report['issues'].append({'code': 'source_asset_expected_native_glyphs_empty',
                            'severity': 'error', 'page': page, 'asset_id': asset.id,
                            'owner_ids': sorted(owners[asset.id]),
                            'detail': 'Selected native identities contain no non-whitespace glyph evidence; an empty comparison cannot certify this asset'})
                        continue
                    path = Path(asset.path)
                    if not path.is_absolute():
                        path = next((root/path for root in roots if (root/path).is_file()), source_root/path)
                    path = path.resolve()
                    if path not in hashes:
                        hashes[path] = file_sha256(path)
                    if hashes[path] != asset.sha256:
                        raise ValueError('Immutable source bundle hash changed')
                    if path != current_path:
                        if current_doc is not None:
                            current_doc.close()
                        current_doc, current_path = fitz.open(path), path
                    actual = _native_counts(current_doc[asset.metadata.get('pdf_page', 0)])
                    missing = expected-actual
                    extra = actual-expected
                    report['checked_assets'] += 1
                    page_result['checked'] += 1
                    report['selected_source_glyphs'] += expected.total()
                    report['extra_extracted_glyphs_not_certified'] += extra.total()
                    if missing:
                        report['missing_glyph_assets'] += 1
                        page_result['missing'] += 1
                        ids = sorted(owners[asset.id])
                        issue = {'code': 'source_asset_native_glyphs_missing', 'severity': 'error',
                                 'page': page, 'asset_id': asset.id, 'owner_ids': ids,
                                 'missing_glyph_count': missing.total(),
                                 'detail': 'Selected source character/font/size identities are absent from the isolated protected asset',
                                 'missing_identities': [{'character': k[0], 'font': k[1], 'size': k[2], 'count': v}
                                                        for k, v in sorted(missing.items())]}
                        if len(ids) == 1:
                            issue['element_id'] = ids[0]
                        report['issues'].append(issue)
                    else:
                        report['passed_assets'] += 1
                        page_result['passed'] += 1
                except (KeyError, ValueError, RuntimeError, OSError, TypeError) as error:
                    report['unverifiable_assets'] += 1
                    page_result['unverifiable'] += 1
                    report['issues'].append({'code': 'source_asset_native_audit_unavailable', 'severity': 'error',
                                             'page': page, 'asset_id': asset.id, 'error_type': type(error).__name__,
                                             'reason': str(error),
                                             'detail': 'Saved source asset could not be verified against the native ledger'})
    finally:
        if current_doc is not None:
            current_doc.close()
    binding_end = audit_binding(book, job_dir)
    report.update(binding_start=binding_start, binding_end=binding_end,
                  binding_unchanged=binding_start == binding_end)
    if not report['binding_unchanged']:
        report['issues'].append({'code': 'source_asset_audit_binding_changed', 'severity': 'error',
                                'detail': 'Source contract, owned asset identities or actual evidence bytes changed during the audit; rerun against one immutable source generation'})
    report['all_eligible_glyph_counts_present'] = ((report['eligible_assets'] == report['checked_assets']
        and not report['missing_glyph_assets'] and not report['unverifiable_assets'] and report['binding_unchanged'])
        if report['eligible_assets'] else None)
    report['status'] = ('not_applicable' if not report['eligible_assets'] else
                        'passed' if report['all_eligible_glyph_counts_present'] else 'blocked')
    report['seconds'] = round(time.perf_counter()-started, 3)
    (output_dir/'source-native-glyph-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('book', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    book = Book.load(args.book)
    report = audit_native_assets(book, args.book.parent, args.out)
    print(json.dumps({k: v for k, v in report.items() if k not in {'issues', 'page_results', 'limitations'}}, ensure_ascii=False))
    return 0 if report['all_eligible_glyph_counts_present'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
