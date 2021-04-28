#
# Copyright (c) nexB Inc. and others. All rights reserved.
# ScanCode is a trademark of nexB Inc.
# SPDX-License-Identifier: Apache-2.0
# See http://www.apache.org/licenses/LICENSE-2.0 for the license text.
# See https://github.com/nexB/scancode-toolkit for support or download.
# See https://aboutcode.org for more information about nexB OSS projects.
#

import io
import sys
from os import environ
from os import path

import attr
from debian_inspector.copyright import CopyrightLicenseParagraph

from debut.copyright import DebianCopyright
from debut.copyright import CatchAllParagraph
from debut.copyright import CopyrightFilesParagraph
from debut.copyright import CopyrightHeaderParagraph
from debut.copyright import is_machine_readable_copyright
from license_expression import LicenseSymbolLike, Licensing
from licensedcode.match_aho import get_matched_starts

from packagedcode.debian import DebianPackage
from packagedcode.licensing import get_normalized_expression
from packagedcode.utils import combine_expressions

from textcode.analysis import unicode_text

"""
Detect licenses in Debian copyright files. Can handle dep-5 machine-readable
copyright files, pre-dep-5 mostly machine-readable copyright files and
unstructured copyright files.
"""

TRACE = environ.get('SCANCODE_DEBUG_PACKAGE', False) or False


def logger_debug(*args):
    pass


if TRACE:
    import logging

    logger = logging.getLogger(__name__)
    # logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
    logging.basicConfig(stream=sys.stdout)
    logger.setLevel(logging.DEBUG)

    def logger_debug(*args):
        return logger.debug(' '.join(isinstance(a, str) and a or repr(a) for a in args))


def get_and_set_package_licenses_and_copyrights(package, root_dir):
    """
    Return a tuple of (declared license, license_expression, copyrights) strings computed
    from the DebianPackage `package` installed in the `root_dir` root directory.
    The package is also updated in place with declared license and license_expression

    For each copyright file paragraph we treat the "name" as a license declaration.
    The text is used for detection and cross-reference with the declaration.
    """
    assert isinstance(package, DebianPackage)
    location = package.get_copyright_file_path(root_dir)

    results = parse_copyright_file(location)
    declared_license, detected_license, copyrights = results

    package.license_expression = detected_license
    package.declared_license = declared_license
    package.copyright = copyrights

    return declared_license, detected_license, copyrights


@attr.s
class DebianCopyrightFileProcessor:

    location = attr.ib()
    is_structured = attr.ib(default=True)

    # List of strings of License: tag values in a structured debian copyright file
    declared_licenses = attr.ib(default=attr.Factory(list))

    # ToDo: Consider returning Line Positions
    # List of strings of Copyright: tag values in a structured debian copyright file
    # or the detected copyrights in an unstructured file
    detected_copyrights = attr.ib(default=attr.Factory(list))
    
    # List of LicenseMatch objects
    license_matches = attr.ib(default=attr.Factory(list))

    @property
    def detected_copyrights_text(self):
        return '\n'.join(self.detected_copyrights)

    @classmethod
    def from_file(cls, location, with_copyright=True, with_debian_packaging=True):
        """
        Return a DebianCopyrightDetector object built from debian copyright file at ``location``,
        or None if this is not a debian copyright file.
        Optionally detect copyright statements, if ``with_copyright`` is True.
        """
        if not location:
            return
        
        if not location.endswith('copyright'):
            return

        dc = cls(location=location)

        content = unicode_text(location)
        dc.is_structured = is_machine_readable_copyright(content)
        
        if with_copyright:
            dc.detected_copyrights = dc.detect_copyrights(
                with_debian_packaging=with_debian_packaging
            )

        if not dc.is_structured:
            dc.license_matches = get_license_matches(location=dc.location)
        else:
            dc.declared_licenses, dc.license_matches = get_structured_licenses(location=dc.location)
        return dc
            
            
    def detect_copyrights(self, with_debian_packaging=False):
        """
        Return copyrights collected from a structured file or an unstructured file.
        """
        copyrights = []
        if self.is_structured:
            deco = DebianCopyright.from_file(self.location)
            for paragraph in deco.paragraphs:
                if is_debian_packaging(paragraph) and not with_debian_packaging:
                    continue
                if isinstance(paragraph, (CopyrightHeaderParagraph, CopyrightFilesParagraph)):
                    pcs = paragraph.copyright.statements or []
                    for p in pcs:
                        p = p.dumps()
                        copyrights.append(p)
        # We detect plain copyrights in a unstructured file if we didn't find any, or
        # in an structured file
        if not copyrights:
            copyrights = copyright_detector(self.location)
            
        return copyrights
    
    @property
    def license_expressions(self):
        """
        Return a list of license expressions.
        """
        matches = self.license_matches
        if not matches:
            # we have no match: return an unknown key
            return ['unknown']

        detected_expressions = [match.rule.license_expression for match in matches]
        return detected_expressions


def parse_copyright_file(
    location,
    with_debian_packaging=True
):
    """
    Return a tuple of (declared license, detected license_expression, copyrights) strings computed
    from the `location` location. For each copyright file paragraph we
    treat the "name" as a license declaration. The text is used for detection
    and cross-reference with the declaration.
    """
    if not location:
        return None, None, None

    if not location.endswith('copyright'):
        return None, None, None

    dc = DebianCopyrightFileProcessor.from_file(
        location=location, with_debian_packaging=with_debian_packaging,
    )

    declared_license = None
    copyrights = dc.detected_copyrights_text

    if not dc.is_structured:
        detected_license = combine_expressions(dc.license_expressions)
    else:
        declared_license, detected_license = parse_structured_copyright_file(
            location=location, with_debian_packaging=with_debian_packaging
        )

    if TRACE:
        logger_debug(
            f'parse_copyright_file: declared_license: {declared_license}\n'
            f'detected_license: {detected_license}\n'
            f'copyrights: {copyrights}'
        )

    return declared_license, detected_license, copyrights


def copyright_detector(location):
    """
    Return lists of detected copyrights, authors & holders in file at location.
    """
    if location:
        from cluecode.copyrights import detect_copyrights
        copyrights = []
        copyrights_append = copyrights.append

        for dtype, value, _start, _end in detect_copyrights(location):
            if dtype == 'copyrights':
                copyrights_append(value)
        return copyrights


def get_license_matches(location=None, query_string=None):
    """
    Return a sequence of LicenseMatch objects.
    """
    from licensedcode import cache

    idx = cache.get_index()
    return idx.match(location=location, query_string=query_string)


class NoLicenseFoundError(Exception):
    """
    Raised when some license is expected to be found, but is not found.
    """


def clean_expression(text):
    """
    Return a cleaned license expression text by normalizing the syntax so it can be parsed.
    """
    # TODO: Check all possible occurances of these
    transforms = {
        ' and, ': ' and ',
        '|': ' or ',
    }
    text = ' '.join(text.split())
    text = text.strip(',')
    for source, target in transforms.items():
        text = text.replace(source, target)

    return text


# TODO: Make it a mapping of license matches
# TODO: Create all these rules, with Relevance: 100, 'bsd' a bit less
# These are based on `/usr/share/common-license/`
common_licenses = {
    'apache-2.0': 'apache-2.0',
    'apache-2.0+': 'apache-2.0',
    'artistic': 'artistic-perl-1.0',
    'bsd': 'bsd-new',
    'cc0-1.0': 'cc0-1.0',
    'gfdl+': 'gfdl-1.1-plus',
    'gfdl-1.2+': 'gfdl-1.2-plus',
    'gfdl-1.3+': 'gfdl-1.3-plus',
    'gpl+': 'gpl-1.0-plus',
    'gpl-1+': 'gpl-1.0-plus',
    'gpl-2+': 'gpl-2.0-plus',
    'gpl-3+': 'gpl-3.0-plus',
    'lgpl+': 'lgpl-2.0-plus',
    'lgpl-2+': 'lgpl-2.0-plus',
    'lgpl-2.1+': 'lgpl-2.1-plus',
    'lgpl-3+': 'lgpl-3.0-plus',
    'gfdl': 'gfdl-1.1-plus',
    'gfdl-1.2': 'gfdl-1.2',
    'gfdl-1.3': 'gfdl-1.3',
    'gpl': 'gpl-1.0-plus',
    'gpl-1': 'gpl-1.0',
    'gpl-2': 'gpl-2.0',
    'gpl-3': 'gpl-3.0',
    'lgpl': 'lgpl-2.0-plus',
    'lgpl-2': 'lgpl-2.0',
    'lgpl-2.1': 'lgpl-2.1',
    'lgpl-3': 'lgpl-3.0',
    'mpl-1.1': 'mpl-1.1',
    'mpl-2.0': 'mpl-2.0',
    'mpl-1.1+': 'mpl-1.1',
    'mpl-2.0+': 'mpl-2.0',
}

@attr.s
class DebianLicenseSymbol:
    key = attr.ib()
    matches = attr.ib(default=attr.Factory(list))
    is_exception = attr.ib(default=False)



def build_symbols(known_license_matches_by_symbol, common_licenses=common_licenses):
    """
    Return a list of LicenseSymbolLike objects, built from known and common licenses.
    It is expected that known_license_matches_by_symbol keys are in lowercase.
    """
    symbols = []
    seen_keys = set()
    for key, matches in known_license_matches_by_symbol.items():
        sym = DebianLicenseSymbol(key=key, matches=matches)
        lsym = LicenseSymbolLike(symbol_like=sym)
        symbols.append(lsym)
        seen_keys.add(key)

    for debian_key, _scancode_key in common_licenses.items():
        if debian_key in seen_keys:
            continue
        
        common_license_tag = f'License: {debian_key}'
        matches = get_license_matches(query_string=common_license_tag)
        sym = DebianLicenseSymbol(key=debian_key, matches=matches)
        lsym = LicenseSymbolLike(symbol_like=sym)
        symbols.append(lsym)
        
    return symbols


def parse_structured_copyright_file(
    location,
    with_debian_packaging=False,
):
    """
    Return a tuple of (list of declared license strings, list of detected license matches)
    collected from the debian copyright file at `location`.

    If `with_debian_packaging` is False, the Debian packaging license is skipped if detected.
    
    #TODO: We want to find in a file where in a copyright file a license was found.
    """
    if not location:
        return [], []

    deco = DebianCopyright.from_file(location)

    declared_licenses = []
    license_matches = []

    #TODO: Revisit: is this really needed
    deco = refine_debian_copyright(deco)

    header_para = None
    file_paras = []
    license_paras = []
    other_paras = []

    for paragraph in deco.paragraphs:
        if isinstance(paragraph, CopyrightHeaderParagraph):
            header_para = paragraph
        elif isinstance(paragraph, CopyrightFilesParagraph):
            file_paras.append(paragraph)
        elif isinstance(paragraph, CopyrightLicenseParagraph):
            license_paras.append(paragraph)
        elif isinstance(paragraph, CatchAllParagraph):
            other_paras.append(paragraph)
        else:
            raise Exception(f'Unknown paragraph type in copyright file, location:{location}, paragraph: {paragraph}')
        
    # We need to iterate over the license paragraphs
    # There, we create a mapping which is "known_licenses_by_name": "matches"

    known_license_matches_by_symbol = {}
    license_paras_matches = []
    
    for license_paragraph in license_paras:
        name = license_paragraph.license.name
        name = name and name.lower()
        common_license = name and common_licenses.get(name)
        text = license_paragraph.license.text
        # TODO: Also handle comment fields
        
        # TODO: We should also track line numbers in the file
        # We should validate and verify the matches are correct
        matches = get_license_matches(query_string=text)

        if common_license:
            # For common license the name has a meaning, so create a synthetic match on that
            for match in matches:
                # FIXME: We have a discrepancy, that needs to be fixed
                if common_license != match.rule.license_expression:
                    pass
            
            common_license_tag = f'License: {name}'
            # TODO: We should also track line numbers in the file
            common_license_matches = get_license_matches(query_string=common_license_tag)
            common_license_matches.extend(matches)
            matches = common_license_matches

        # Refactor me as license_paras_matches.extend(matches) is duplicated
        if name in known_license_matches_by_symbol:
            # FIXME: There are two instances of the same key, needs to be fixed
            license_paras_matches.extend(matches)
        elif not name:
            # FIXME: License text exists without the name
            license_paras_matches.extend(matches)
        else:
            known_license_matches_by_symbol[name] = matches
            license_paras_matches.extend(matches)
        
    expression_symbols = build_symbols(
        known_license_matches_by_symbol=known_license_matches_by_symbol
    )
    licensing = Licensing(symbols=expression_symbols)
    file_paras_matches = []
    for file_paragraph in file_paras:
        text = file_paragraph.license.text
        # TODO: Also handle comment fields
        # TODO: We should also track line numbers in the file
        # We should validate and verify the matches are correct
        matches = get_license_matches(query_string=text)
        
        name = file_paragraph.license.name
        if not name:
            if not matches:
                # TODO: add an unknown match
                pass
            else:
                file_paras_matches.extend(matches)
            continue

        # is the name a single common license?
        # is the name a known license from the license paragraph
        # If the name is a license expression with known/common license
        # Validate the matches at text level against the name level
        cleaned = clean_expression(text=name)
        

        

    licensing = Licensing()
    for paragraph in deco.paragraphs:

        if is_debian_packaging(paragraph) and not with_debian_packaging:
            # Skipping packaging license since they are not
            # relevant to the effective package license
            continue

        # rare case where we have not a structured file
        if isinstance(paragraph, CatchAllParagraph):
            text = paragraph.dumps()
            if text:
                matches = get_license_matches(query_string=text)
                if not matches:
                    raise NoLicenseFoundError(text)
                license_matches.extend(matches)
            continue
        
        plicense = paragraph.license
        if not plicense:
            continue

        declared, detected = detect_declared_license(plicense.name)
        # avoid repeats
        if declared:
            declared_licenses.append(declared)
        if detected:
            license_matches.append(detected)

        # also detect in text
        text = paragraph.license.text
        if text:
            detected = get_normalized_expression(
                text,
                try_as_expression=False,
                approximate=True,
            )
            if not detected:
                detected = 'unknown'
            license_matches.append(detected)

    declared_license = '\n'.join(declared_licenses)

    if license_matches:
        detected_licenses = [licensing.parse(dl, simple=True) for dl in license_matches]

        if len(detected_licenses) > 1:
            detected_license = licensing.AND(*detected_licenses)
        else:
            detected_license = detected_licenses[0]

        detected_license = str(detected_license)

    else:
        detected_license = 'unknown'

    return declared_license, detected_license


def detect_declared_license(declared):
    """
    Return a tuple of (declared license, detected license expression) from a
    declared license. Both can be None.
    """
    declared = normalize_and_cleanup_declared_license(declared)

    if TRACE:
        logger_debug(f'detect_declared_license: {declared}')

    if not declared:
        return None, None

    # apply multiple license detection in sequence
    detected = detect_using_name_mapping(declared)
    if detected:
        return declared, detected

    from packagedcode import licensing
    try:
        detected = licensing.get_normalized_expression(
            declared,
            try_as_expression=False,
            approximate=False,
        )
    except Exception:
        # FIXME: add logging
        # we never fail just for this
        return 'unknown'

    return declared, detected


def normalize_and_cleanup_declared_license(declared):
    """
    Return a cleaned and normalized declared license.
    """
    declared = declared or ''
    # there are few odd cases of license fileds starting with a colon or #
    declared = declared.strip(': \t#')
    # normalize spaces
    declared = ' '.join(declared.split())
    return declared


def detect_using_name_mapping(declared):
    """
    Return a license expression detected from a declared_license.
    """
    declared = declared.lower()
    detected = get_declared_to_detected().get(declared)
    if detected:
        licensing = Licensing()
        return str(licensing.parse(detected, simple=True))


def is_debian_packaging(paragraph):
    """
    Return True if the `paragraph` is a CopyrightFilesParagraph that applies
    only to the Debian packaging
    """
    return (
        isinstance(paragraph, CopyrightFilesParagraph)
        and paragraph.files == ['debian/*']
    )


def is_primary_license_paragraph(paragraph):
    """
    Return True if the `paragraph` is a CopyrightFilesParagraph that contains
    the primary license.
    """
    return (
        isinstance(paragraph, CopyrightFilesParagraph)
        and paragraph.files == ['*']
    )


def refine_debian_copyright(debian_copyright):
    """
    Update in place the `debian_copyright` DebianCopyright object based on
    issues found in a large collection of Debian copyright files.
    """
    for paragraph in debian_copyright.paragraphs:
        if not hasattr(paragraph, 'license'):
            continue
        plicense = paragraph.license
        if not plicense:
            continue

        license_name = plicense.name
        if not license_name:
            continue

        if license_name.startswith('200'):
            # these are copyrights and not actual licenses, such as:
            # - 2005 Sergio Costas
            # - 2006-2010 by The HDF Group.

            if isinstance(paragraph, (CopyrightHeaderParagraph, CopyrightFilesParagraph)):
                pcs = paragraph.copyright.statements or []
                pcs.append(license_name)
                paragraph.copyright.statements = pcs
                paragraph.license.name = None

        license_name_low = license_name.lower()
        NOT_A_LICENSE_NAME = (
            'according to',
            'by obtaining',
            'distributed under the terms of the gnu',
            'gnu general public license version 2 as published by the free',
            'gnu lesser general public license 2.1 as published by the',
        )
        if license_name_low.startswith(NOT_A_LICENSE_NAME):
            text = plicense.text
            if text:
                text = '\n'.join([license_name, text])
            else:
                text = license_name
            paragraph.license.name = None
            paragraph.license.text = text

    return debian_copyright


_DECLARED_TO_DETECTED = None


def get_declared_to_detected(data_file=None):
    """
    Return a mapping of declared to detected license expression cached and
    loaded from a tab-separated text file, all lowercase.

    Each line has this form:
        some license name<tab>scancode license expression

    For instance:
        2-clause bsd    bsd-simplified

    This data file is about license keys used in copyright files and has been
    derived from a large collection of most copyright files from Debian (about
    320K files from circa 2019-11) and Ubuntu (about 200K files from circa
    2020-06)
    """
    global _DECLARED_TO_DETECTED
    if _DECLARED_TO_DETECTED:
        return _DECLARED_TO_DETECTED

    _DECLARED_TO_DETECTED = {}
    if not data_file:
        data_file = path.join(path.dirname(__file__), 'debian_licenses.txt')
    with io.open(data_file, encoding='utf-8') as df:
        for line in df:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            decl, _, detect = line.strip().partition('\t')
            if detect and detect.strip():
                decl = decl.strip()
                _DECLARED_TO_DETECTED[decl] = detect
    return _DECLARED_TO_DETECTED
