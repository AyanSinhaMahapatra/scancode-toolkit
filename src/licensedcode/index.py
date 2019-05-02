# -*- coding: utf-8 -*-
#
# Copyright (c) 2018 nexB Inc. and others. All rights reserved.
# http://nexb.com and https://github.com/nexB/scancode-toolkit/
# The ScanCode software is licensed under the Apache License version 2.0.
# Data generated with ScanCode require an acknowledgment.
# ScanCode is a trademark of nexB Inc.
#
# You may not use this software except in compliance with the License.
# You may obtain a copy of the License at: http://apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
# When you publish or redistribute any data created with ScanCode or any ScanCode
# derivative work, you must accompany this data with the following acknowledgment:
#
#  Generated with ScanCode and provided on an "AS IS" BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, either express or implied. No content created from
#  ScanCode should be considered or used as legal advice. Consult an Attorney
#  for any legal advice.
#  ScanCode is a free software code scanning tool from nexB Inc. and others.
#  Visit https://github.com/nexB/scancode-toolkit/ for support and download.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from array import array
from collections import Counter
from collections import defaultdict
import cPickle
from functools import partial
from operator import itemgetter
import pickle
import sys
from time import time

# Python 2 and 3 support
try:
    import itertools.izip as zip  # NOQA
except ImportError:
    pass

from intbitset import intbitset

from commoncode.dict_utils import sparsify
from licensedcode import MAX_DIST
from licensedcode.frequent_tokens import global_tokens_by_ranks
from licensedcode import match
from licensedcode import match_aho
from licensedcode import match_hash
from licensedcode import match_seq
from licensedcode import match_set
from licensedcode import match_spdx_lid
from licensedcode import query
from licensedcode import tokenize


"""
Main license index construction, query processing and matching entry points for
license detection.
The LicenseIndex is the main class and holds the index structures and the
`match` method drives the matching as a succession of matching strategies.
Actual matching is delegated to other modules that implement a matching
strategy.
"""

# Tracing flags
TRACE = False
TRACE_MATCHES = False
TRACE_MATCHES_TEXT = False
TRACE_NEGATIVE = False
TRACE_INDEXING_PERF = False
TRACE_TOKEN_DOC_FREQ = False


def logger_debug(*args):
    pass


if (TRACE
or TRACE_MATCHES
or TRACE_NEGATIVE
or TRACE_INDEXING_PERF):

    import logging

    logger = logging.getLogger(__name__)
    # logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
    logging.basicConfig(stream=sys.stdout)
    logger.setLevel(logging.DEBUG)

    def logger_debug(*args):
        return logger.debug(' '.join(isinstance(a, basestring) and a or repr(a)
                                     for a in args))


# Feature switch to enable or not ngram fragments detection
# FIXME: this is not used
USE_AHO_FRAGMENTS = False

# length of ngrams used for fragments detection
NGRAM_LEN = 6

# Maximum number of unique tokens we can handle: 16 bits signed integers are up to
# 32767. Since we use internally several arrays of ints for smaller and optimized
# storage we cannot exceed this number of tokens.
MAX_TOKENS = (2 ** 15) - 1

# If 1/4, 1/4 of all tokens will be treated as junk
PROPORTION_OF_JUNK = 0.7


class LicenseIndex(object):
    """
    A license detection index. An index is queried for license matches found in
    a query file. The index support multiple strategies for finding exact and
    approximate matches.
    """
    # slots are not really needed but they help with sanity and avoid an
    # unchecked proliferation of new attributes
    __slots__ = (
        'len_tokens',
        'len_junk',
        'len_good',
        'dictionary',
        'digit_only_tids',
        'tokens_by_tid',

        'tokens_idf_by_tid',

        'rules_by_rid',
        'tids_by_rid',

        'high_postings_by_rid',

        'tids_all_sets_by_rid',
        'tids_all_msets_by_rid',

        'rid_by_hash',
        'rules_automaton',
        'fragments_automaton',
        'negative_automaton',

        'regular_rids',
        'negative_rids',
        'false_positive_rids',
        'approx_matchable_rids',

        'optimized',
    )

    def __init__(self, rules=None, _ranked_tokens=global_tokens_by_ranks,
                 _spdx_tokens=None):
        """
        Initialize the index with an iterable of Rule objects.
        """
        # total number of unique known tokens
        self.len_tokens = 0

        # largest token ID for a "junk" token. A token with a smaller id than
        # len_junk is considered a "junk" very common token
        self.len_junk = 0
        # corresponding number of non-junk tokens: len_tokens = len_junk + len_good
        self.len_good = 0

        # mapping of token string > token id
        self.dictionary = {}

        # set of token ids made entirely of digits
        self.digit_only_tids = set()

        # mapping-like of token id -> token string as a list where the index is the
        # token id and the value the actual token string.
        # This the reverse of the dictionary.
        self.tokens_by_tid = []

        # mapping-like of token id -> token IDF (inverse document frequency) as a
        # list where the index is the token id and the value is the IDF (inverse
        # document occurence frequency) as a float.
        self.tokens_idf_by_tid = []

        # Note: all the following are mappings-like (using lists) of
        # rid-> data are lists of data where the index is the rule id.

        # maping-like of rule_id -> rule objects proper
        self.rules_by_rid = []

        # maping-like of rule_id -> sequence of token_ids
        self.tids_by_rid = []

        # mapping-like of rule id->(mapping of (token_id->[positions, ...])
        # We track only high/good tokens there. This is a "traditional"
        # inverted index postings list
        self.high_postings_by_rid = []

        # mapping-like of rule_id -> tokens ids sets/multisets
        self.tids_all_sets_by_rid = []
        self.tids_all_msets_by_rid = []

        # mapping of hash -> single rid for hash match: duplicated rules are not allowed
        self.rid_by_hash = {}

        # Aho-Corasick automatons for regular and negative rules
        self.rules_automaton = match_aho.get_automaton()
        if USE_AHO_FRAGMENTS:
            self.fragments_automaton = match_aho.get_automaton()
        else:
            self.fragments_automaton = None
        self.negative_automaton = match_aho.get_automaton()

        # disjunctive sets of rule ids: regular, negative, false positive, and
        # things that can only be matched exactly

        # TODO: consider using intbitset instead
        self.regular_rids = set()
        self.negative_rids = set()
        self.false_positive_rids = set()
        # these rule ids are for rules that can be matched with a sequence match
        self.approx_matchable_rids = set()

        # if True the index has been optimized and becomes read only:
        # no new rules can be added
        self.optimized = False

        if rules:
            if TRACE_INDEXING_PERF:
                start = time()
                print('LicenseIndex: building index.')

            # index all and optimize
            self._add_rules(rules, _ranked_tokens, _spdx_tokens)

            if TRACE_TOKEN_DOC_FREQ:
                print('LicenseIndex: token, frequency')
                from itertools import chain
                tokens_by_tid = self.tokens_by_tid
                regular_rids = self.regular_rids
                tokens_idf_by_tid = self.tokens_idf_by_tid
                tf = Counter(chain.from_iterable(
                    tids for rid, tids in enumerate(self.tids_by_rid)
                    if rid in regular_rids))

                for tid, freq in tf.most_common():
                    ts = tokens_by_tid[tid]
                    idf = tokens_idf_by_tid[tid]
                    print(u'"{ts}","{freq}","{idf}"'.format(**locals()))

            if TRACE_INDEXING_PERF:
                duration = time() - start
                len_rules = len(self.rules_by_rid)
                print('LicenseIndex: built index with %(len_rules)d rules in %(duration)f seconds.' % locals())
                self._print_index_stats()

    def _add_rules(self, rules, _ranked_tokens=global_tokens_by_ranks,
                   _spdx_tokens=None):
        """
        Add a list of Rule objects to the index and constructs optimized and
        immutable index structures.

        `_spdx_tokens` if provided is a set of token strings from known SPDX
        keys: these receive a special treatment.
        """
        if self.optimized:
            raise Exception('Index has been optimized and cannot be updated.')

        # this assigns the rule ids implicitly: this is the index in the list
        self.rules_by_rid = list(rules)

        #######################################################################
        # classify rules, collect tokens and frequencies
        #######################################################################
        # accumulate all rule tokens strings. This is used only during indexing
        token_strings_by_rid = []
        token_strings_by_rid_append = token_strings_by_rid.append

        # count token strings globally
        token_strings_count = defaultdict(int)
        # token_strings_count_update = token_strings_count.update

        # OPTIMIZATION: bind frequently used methods to the local scope
        false_positive_rids_add = self.false_positive_rids.add
        negative_rids_add = self.negative_rids.add
        regular_rids_add = self.regular_rids.add

        for rid, rul in enumerate(self.rules_by_rid):
            rul.rid = rid

            rul_tokens = list(rul.tokens())
            token_strings_by_rid_append(rul_tokens)
            for t in rul_tokens:
                token_strings_count[t] += 1

            # classify rules and build disjuncted sets of rids
            if rul.is_false_positive:
                # false positive rules do not participate in the matches at all
                # they are used only in post-matching filtering
                false_positive_rids_add(rid)
            elif rul.is_negative:
                # negative rules are matched early and their exactly matched.
                # Their tokens are removed from the token stream
                negative_rids_add(rid)
            else:
                # regular rules are matched using the standard approach
                regular_rids_add(rid)

        # Add SPDX key tokens to the dictionary. track which are only from SPDX leys
        ########################################################################
        spdx_tokens = None
        if _spdx_tokens:
            spdx_tokens = _spdx_tokens.difference(token_strings_count)
            for t in _spdx_tokens:
                token_strings_count[t] += 1

        # Create the tokens lookup structure at once. Note that tokens ids are
        # assigned randomly here at first by unzipping: we get the frequencies
        # and tokens->id at once this way
        ########################################################################
        tokens_by_tid, tokens_count_by_tid = zip(*token_strings_count.items())
        self.tokens_by_tid = tokens_by_tid
        self.len_tokens = len_tokens = len(tokens_by_tid)
        msg = 'Cannot support more than licensedcode.index.MAX_TOKENS: %d' % MAX_TOKENS
        assert len_tokens <= MAX_TOKENS, msg

        # initial dictionary mapping to old/arbitrary token ids
        ########################################################################
        self.dictionary = dictionary = {ts: tid for tid, ts in enumerate(tokens_by_tid)}
        # OPTIMIZED
        sparsify(dictionary)

        # replace token strings with arbitrary (and temporary) integer ids
        ########################################################################
        self.tids_by_rid = [[dictionary[tok] for tok in rule_tok]
                            for rule_tok in token_strings_by_rid]

        # Get SPDX-only token ids
        ########################################################################
        spdx_token_ids = None
        if spdx_tokens:
            spdx_token_ids = set(dictionary[tok] for tok in spdx_tokens)

        #######################################################################
        # renumber token ids based on frequencies and common words aka. "frequent_tokens"
        #######################################################################
        len_junk, self.dictionary, self.tokens_by_tid, self.tids_by_rid = self.renumber_token_ids(
            tokens_count_by_tid, _ranked_tokens, _spdx_token_ids=spdx_token_ids)

        # Some rules cannot be matched as a sequence: we exclude "weak" rules as
        # not "approx" matchable These weak rules made entirely of junk tokens:
        # they can only be matched using an automaton
        weak_rids = get_weak_rids(len_junk, self.tids_by_rid, _idx=self)
        self.approx_matchable_rids = self.regular_rids.difference(weak_rids)

        # some tokens are made entirely of digits and these can create some
        # worst case behavior when there are long runs on these
        self.digit_only_tids = intbitset([
            i for i, s in enumerate(self.tokens_by_tid) if s.isdigit()])

        #######################################################################
        # build index structures
        #######################################################################
        self.len_junk = len_junk
        self.len_good = len_tokens - len_junk
        len_rules = len(self.rules_by_rid)

        # a count in how many doc a tokens shows into
        tokens_doc_freq_by_tid = [0 for _ in range(len_tokens)]

        # since we only use these for regular rules, these lists may be sparse.
        # their index is the rule rid
        self.high_postings_by_rid = high_postings_by_rid = [None] * len_rules
        self.tids_all_sets_by_rid = tids_all_sets_by_rid = [None] * len_rules
        self.tids_all_msets_by_rid = tids_all_msets_by_rid = [None] * len_rules

        # track all duplicate rules: fail and report dupes at once at the end
        dupe_rules_by_hash = defaultdict(list)

        # build closures for methods that populate automatons
        negative_automaton_add = partial(
            match_aho.add_sequence, automaton=self.negative_automaton)

        if USE_AHO_FRAGMENTS:
            fragments_automaton_add = partial(
                match_aho.add_sequence, automaton=self.fragments_automaton)
        else:
            fragments_automaton_add = lambda x: x

        rules_automaton_add = partial(
            match_aho.add_sequence, automaton=self.rules_automaton)

        # OPTIMIZED: bind frequently used objects to local scope
        rules_by_rid = self.rules_by_rid
        rid_by_hash = self.rid_by_hash
        match_hash_index_hash = match_hash.index_hash
        match_set_tids_set_counter = match_set.tids_set_counter
        match_set_tids_multiset_counter = match_set.tids_multiset_counter

        #######################################################################
        # build by-rule index structures over the token ids seq of each rule
        #######################################################################
        for rid, rule_token_ids in enumerate(self.tids_by_rid):
            rule = rules_by_rid[rid]

            if TRACE and rule.identifier == 'public-domain_this.RULE':
                logger_debug('by-rule index structure', rule)
            # build hashes index and check for duplicates rule texts
            rule_hash = match_hash_index_hash(rule_token_ids)
            dupe_rules_by_hash[rule_hash].append(rule)

            if rule.is_negative:
                negative_automaton_add(tids=rule_token_ids, rid=rid)
                continue

            # update hashes index
            rid_by_hash[rule_hash] = rid

            rule.is_approx_matchable = is_approx_matchable = rid not in weak_rids

            # no postings for rules that cannot be matched as a sequence (too short and weak)
            if is_approx_matchable:
                ####################
                # update high postings index: positions by high tids used to
                # speed up sequence matching
                ####################
                # TODO: this could be optimized with a group_by
                postings = defaultdict(list)
                for pos, tid in enumerate(rule_token_ids):
                    if tid >= len_junk:
                        postings[tid].append(pos)
                # OPTIMIZED: for speed and memory: convert postings to arrays
                postings = {tid: array('h', value) for tid, value in postings.items()}
                # OPTIMIZED: for speed, sparsify dict
                sparsify(postings)
                high_postings_by_rid[rid] = postings

                ####################
                # ... and ngrams: compute ngrams and populate an automaton with ngrams
                ####################
                if (USE_AHO_FRAGMENTS and rule.minimum_coverage < 100 and rule.length > NGRAM_LEN):

                    all_ngrams = tokenize.ngrams(rule_token_ids, ngram_length=NGRAM_LEN)
                    all_ngrams_with_pos = tokenize.select_ngrams(all_ngrams, with_pos=True)
#                     all_ngrams_with_pos = enumerate(all_ngrams)
                    for pos, ngram in all_ngrams_with_pos:
                        fragments_automaton_add(tids=ngram, rid=rid, start=pos)

            ####################
            # build sets and multisets indexes, for all regular rules as we need the thresholds
            ####################
            tids_set, tids_mset = match_set.index_token_sets(rule_token_ids)
            tids_all_msets_by_rid[rid] = tids_mset
            tids_all_sets_by_rid[rid] = tids_set

            ####################################################################
            ####################################################################
            # FIXME!!!!!!! we should store them if we need them
            tids_set_high = match_set.high_tids_set_subset(tids_set, len_junk)
            tids_mset_high = match_set.high_tids_multiset_subset(tids_mset, len_junk)


            # FIXME!!!!!!!
            ####################################################################
            ####################################################################

            ####################
            # update rule thresholds
            ####################
            rule.length_unique = match_set_tids_set_counter(tids_set)
            rule.high_length_unique = match_set_tids_set_counter(tids_set_high)

            rule.high_length = match_set_tids_multiset_counter(tids_mset_high)
            rule.compute_thresholds()

            ####################
            # update idf counters
            ####################
            for tid in tids_set:
                tokens_doc_freq_by_tid[tid] += 1

            ####################
            # populate automaton with the whole rule tokens sequence, for
            # all RULEs, be they "standard", weak or small (but not negative
            ####################
            rules_automaton_add(tids=rule_token_ids, rid=rid)

        # finalize automatons
        self.negative_automaton.make_automaton()
        self.rules_automaton.make_automaton()
        if USE_AHO_FRAGMENTS:
            self.fragments_automaton.make_automaton()

        # finalize tokens IDF (inverse documents frequency)
        self.tokens_idf_by_tid = match_set.compute_idfs(len_rules, tokens_doc_freq_by_tid)

        # OPTIMIZED: sparser dicts for faster lookup
        sparsify(self.rid_by_hash)

        dupe_rules = [rules for rules in dupe_rules_by_hash.values() if len(rules) > 1]
        if dupe_rules:
            dupe_rule_paths = [
                '\n'.join(
                    sorted([('file://' + rule.text_file) if rule.text_file else ('text: ' + rule.stored_text)
                            for rule in rules])
                    )
                for rules in dupe_rules
            ]
            msg = ('Duplicate rules: \n' + '\n\n'.join(dupe_rule_paths))
            raise AssertionError(msg)

        self.optimized = True

    def debug_matches(self, matches, message, location=None, query_string=None,
                      with_text=False, query=None):
        """
        Log debug-level data for a list of `matches`.
        """
        if TRACE or TRACE_NEGATIVE:
            logger_debug(message + ':', len(matches))
            if query:
                # set line early to ease debugging
                match.set_lines(matches, query.line_by_pos)

            if TRACE_MATCHES or TRACE_NEGATIVE:
                map(logger_debug, matches)

            if (TRACE_MATCHES_TEXT or TRACE_NEGATIVE) and with_text:
                logger_debug(message + ' MATCHED TEXTS')

                from licensedcode.tracing import get_texts

                for m in matches:
                    logger_debug(m)
                    qt, it = get_texts(m)
                    print('  MATCHED QUERY TEXT:', qt)
                    print('  MATCHED RULE TEXT:', it)
                    print()

    def get_spdx_id_matches(self, query, from_spdx_id_lines=True, **kwargs):
        """
        Matching strategy for SPDX-Licensed-Identifier style of expressions. If
        `from_spdx_id_lines` is True detect only in the SPDX license identifier
        lines found in the query. Otherwise use the whole query for detection.
        """
        matches = []

        if from_spdx_id_lines:
            qrs_and_texts = query.spdx_lid_query_runs_and_text()
        else:
            # if we are not specifically looking at a single SPDX-Licene-
            # identifier line, then use the whole query run with the whole text
            # note this can only work for small texts or this will make the
            # experssion parser choke
            query_lines = [ln for _, ln
                in tokenize.query_lines(query.location, query.query_string)]
            qrs_and_texts = query.whole_query_run(), u'\n'.join(query_lines)
            qrs_and_texts = [qrs_and_texts]

        for query_run, detectable_text in qrs_and_texts:
            if not query_run.matchables:
                # this could happen if there was some negative match applied
                continue
            spdx_match = match_spdx_lid.spdx_id_match(
                self, query_run, detectable_text)
            query_run.subtract(spdx_match.qspan)
            matches.append(spdx_match)

        return matches

    def get_exact_matches(self, query, **kwargs):
        """
        Extract matching strategy using an automaton for multimatching at once.
        """
        wqr = query.whole_query_run()
        matches = match_aho.exact_match(self, wqr, self.rules_automaton)
        matches, _discarded = match.refine_matches(matches, self,
            query=query, filter_false_positive=False, merge=False)
        return matches

    def get_fragments_matches(self, query, matched_qspans, **kwargs):
        """
        Approximate matching strategy breaking a query in query_runs and using
        fragment matching. Return a list of matches.
        """
        matches = []

        for query_run in query.query_runs:
            # we cannot do a sequence match in query run without some high token left
            if not query_run.is_matchable(include_low=False, qspans=matched_qspans):
                continue
            qrun_matches = match_aho.match_fragments(self, query_run)
            matches.extend(match.merge_matches(qrun_matches))

        return matches

    def get_approximate_matches(self, query, matched_qspans, existing_matches, **kwargs):
        """
        Approximate matching strategy breaking a query in query_runs and using
        multiple local alignments (aka. diff). Return a list of matches.
        """
        matches = []

        for query_run in query.query_runs:

            # we cannot do a sequence match in query run without some high token left
            if not query_run.is_matchable(include_low=False, qspans=matched_qspans):
                continue

            # collect rules already matched exactly withing that query run: we
            # do not need to try to rematch these
            mrid = set(get_matched_rule_ids(existing_matches, query_run))
            matchable_rids = self.approx_matchable_rids.difference(mrid)

            # inverted index match and ranking, query run-level
            # FIXME: we should consider aho matches to excludes them from candidates
            # FIXME: also exclude from candidates any rule that is only aho-matchable
            qrun_matches = []
            MAX_CANDIDATES = 65
            candidates = match_set.compute_candidates(
                query_run=query_run,
                idx=self,
                matchable_rids=matchable_rids,
                top=MAX_CANDIDATES)

            # multiple sequence matching/alignment for each canidate, query run-
            # level for as long as we have more non-overlapping matches returned
            for candidate_rule in candidates:
                start_offset = 0
                while True:
                    rule_matches = match_seq.match_sequence(
                        self, candidate_rule, query_run, start_offset=start_offset)
                    if not rule_matches:
                        break
                    matches_end = max(m.qend for m in rule_matches)
                    qrun_matches.extend(rule_matches)
                    if matches_end + 1 < query_run.end:
                        start_offset = matches_end + 1
                        continue
                    else:
                        break

            matches.extend(match.merge_matches(qrun_matches))

        return matches

    def match(self, location=None, query_string=None, min_score=0,
              as_expression=False, **kwargs):
        """
        This is the main entry point to match licenses.

        Return a sequence of LicenseMatch by matching the file at `location` or
        the `query_string` text against the index. Only include matches with
        scores greater or equal to `min_score`.

        If `as_expression` is True, treat the whole text as a single SPDX
        license expression and use only expression matching.
        """
        assert 0 <= min_score <= 100

        if not location and not query_string:
            return []

        qry = query.build_query(location, query_string, idx=self,
            text_line_threshold=15, bin_line_threshold=50)

        if not qry:
            return []

        whole_query_run = qry.whole_query_run()
        if not whole_query_run or not whole_query_run.matchables:
            return []

        hash_matches = match_hash.hash_match(self, whole_query_run)
        if hash_matches:
            match.set_lines(hash_matches, qry.line_by_pos)
            return hash_matches

        # TODO: add match to degenerated expressions with custom symbols
        if as_expression:
            matches = self.get_spdx_id_matches(qry, from_spdx_id_lines=False)
            match.set_lines(matches, qry.line_by_pos)
            return matches

        negative_matches = []
        if self.negative_rids:
            negative_matches = self.negative_match(whole_query_run)
            for neg in negative_matches:
                whole_query_run.subtract(neg.qspan)
            if TRACE_NEGATIVE:
                self.debug_matches(
                    negative_matches, 'negative_matches', location, query_string)  # , with_text, query)

        matches = []

        if USE_AHO_FRAGMENTS:
            approx = (self.get_fragments_matches, False)
        else:
            approx = (self.get_approximate_matches, False)
        matchers = [
            # matcher, include_low in post-matching remaining matchable check
            (self.get_spdx_id_matches, True),
            (self.get_exact_matches, False),
            approx
        ]

        already_matched_qspans = []
        for matcher, include_low in matchers:
            matched = matcher(qry, matched_qspans=already_matched_qspans, existing_matches=matches)
            if TRACE:
                logger_debug()
                logger_debug('matching with matcher:', matcher)
                self.debug_matches(matched, 'matched', location, query_string)  # , with_text, query)

            matches.extend(matched)
            # check if we have some matchable left
            # do not match futher if we do not need to
            # collect qspans matched exactly e.g. with coverage 100%
            # this coverage check is because we have provision to match fragments (unused for now)
            already_matched_qspans.extend(m.qspan for m in matched if m.coverage() == 100)

            if not whole_query_run.is_matchable(
                include_low=include_low, qspans=already_matched_qspans):
                break

        if not matches:
            return []

        if TRACE:
            logger_debug()
            self.debug_matches(matches, 'matches before final merge',
                               location, query_string, with_text=True, query=qry)

        matches, _discarded = match.refine_matches(
            matches, idx=self, query=qry, min_score=min_score,
            max_dist=MAX_DIST // 2, filter_false_positive=True, merge=True)

        matches.sort()
        match.set_lines(matches, qry.line_by_pos)

        if TRACE:
            logger_debug()
            logger_debug()
            self.debug_matches(matches, 'final matches',
                               location, query_string ,
                               with_text=True, query=qry)
        return matches

    def negative_match(self, query_run):
        """
        Match a query run exactly against negative rules. Return a list
        of negative LicenseMatch for a query run, subtract these matches
        from the query run.
        """
        return match_aho.exact_match(self, query_run, self.negative_automaton)

    def _print_index_stats(self):
        """
        Print internal Index structures stats. Used for debugging and testing.
        """
        try:
            from pympler.asizeof import asizeof as size_of
        except ImportError:
            print('Index statistics will be approximate: `pip install pympler` for correct structure sizes')
            from sys import getsizeof as size_of

        fields = [
            'dictionary',
            'tokens_by_tid',
            'rid_by_hash',
            'rules_by_rid',
            'tids_by_rid',

            'tids_lohi_sets_by_rid',
            'tids_lohi_msets_by_rid',

            'regular_rids',
            'negative_rids',
            'approx_matchable_rids'
            'false_positive_rids',
        ]

        plen = max(map(len, fields)) + 1
        internal_structures = [s + (' ' * (plen - len(s))) for s in fields]

        print('Index statistics:')
        total_size = 0
        for struct_name in internal_structures:
            struct = getattr(self, struct_name.strip())
            try:
                print('  ', struct_name, ':', 'length    :', len(struct))
            except:
                print('  ', struct_name, ':', 'repr      :', repr(struct))
            siz = size_of(struct)
            total_size += siz
            print('  ', struct_name, ':', 'size in MB:', round(siz / (1024 * 1024), 2))
        print('    TOTAL internals in MB:', round(total_size / (1024 * 1024), 2))
        print('    TOTAL real size in MB:', round(size_of(self) / (1024 * 1024), 2))

    def to_dict(self, all_tokens=False):
        """
        Return a human readable dictionary representing the index replacing
        token ids and rule ids with their string values and the postings by
        lists. Used for debugging and testing. Covers only high postings
        """
        dct = {}
        # FIXME: this is not representative and used only for test as a sanity check
        # FIXME: we do not have the low postings
        for rid, postings in enumerate(self.high_postings_by_rid):
            postings = postings or {}
            ridentifier = self.rules_by_rid[rid].identifier
            ridentifier = ridentifier + '_' + str(rid)
            dct[ridentifier] = {
                self.tokens_by_tid[tid]: list(positions)
                for tid, positions in postings.viewitems()}
        return dct

    def _tokens2text(self, tokens):
        """
        Return a text string from a sequence of token ids.
        Used for tracing and debugging.
        """
        return u' '.join('None' if t is None else self.tokens_by_tid[t] for t in tokens)

    @staticmethod
    def loads(saved, fast=True):
        """
        Return a LicenseIndex from a pickled string.
        """
        pickler = cPickle if fast else pickle
        idx = pickler.loads(saved)
        # perform some optimizations on the dictionaries
        sparsify(idx.dictionary)
        return idx

    @staticmethod
    def load(fn, fast=True):
        """
        Return a LicenseIndex loaded from the `fn` file-like object pickled index.
        """
        pickler = cPickle if fast else pickle
        idx = pickler.load(fn)
        # perform some optimizations on the dictionaries
        sparsify(idx.dictionary)
        return idx

    def dumps(self, fast=True):
        """
        Return a pickled string of self.
        """
        # here cPickle fails when we load it back. Pickle is slower to write but
        # works when we read with cPickle :|
        pickler = cPickle if fast else pickle
        pickled = pickler.dumps(self, protocol=cPickle.HIGHEST_PROTOCOL)

        # NB: this is making the usage of cPickle possible... as a weird workaround.
        # the gain from dumping using cPickle is not as big with this optimize
        # but still much faster than using the plain pickle module
        # TODO: revisit me after the Python3 port
        import pickletools
        pickletools.code2op = sparsify(pickletools.code2op)
        pickled = pickletools.optimize(pickled)
        return pickled

    def dump(self, fn, fast=False):
        """
        Dump (write) a pickled self to the `fn` file-like object.
        """
        # here cPickle fails when we load it back. Pickle is slower to write but
        # works when we read with cPickle :|
        pickler = cPickle if fast else pickle
        return pickler.dump(self, fn, protocol=cPickle.HIGHEST_PROTOCOL)

    def renumber_token_ids(self, tokens_count_by_old_tid,
                           _ranked_tokens=global_tokens_by_ranks,
                           _spdx_token_ids=None):
        """
        Return updated index structures with new token ids such that the most
        common tokens (aka. 'junk' or 'low' tokens) have the lowest ids.

        Return a tuple of (len_junk, dictionary, tokens_by_tid, tids_by_rid)
        - len_junk: the number of junk_old_tids tokens such that all junk token
          ids are smaller than this number.
        - dictionary: mapping of token string->token id
        - tokens_by_tid: reverse mapping of token id->token string
        - tids_by_rid: mapping of rule id-> array of token ids

        The arguments all relate to old, temporary token ids and are:
        - `tokens_count_by_old_tid`: mapping of token id-> token occurences
           count across all rules.
        - `_ranked_tokens`: callable returning a list of common lowercase token
          strings, ranked from most common to least common.
        - `_spdx_token_ids` if provided is a set of token ids that are only
          comming from SPDX keys and are not otherwise present in other license
          rules.

        Common tokens are computed based on a curated list of frequent words and
        token frequencies across rules such that common (aka. junk) tokens have
        lower token ids strictly smaller than len_junk.
        """
        old_dictionary = self.dictionary
        tokens_by_old_tid = self.tokens_by_tid
        old_tids_by_rid = self.tids_by_rid

        # track tokens for rules with a single token: their token is never
        # common/junk otherwise they can never be detected
        rules_of_one = set(r.rid for r in self.rules_by_rid if r.length == 1)
        never_junk_old_tids = set(rule_tokens[0] for rid, rule_tokens
                                  in enumerate(old_tids_by_rid)
                                  if rid in rules_of_one)

        # create initial set of common/junk token ids
        junk_old_tids = set()
        junk_old_tids_add = junk_old_tids.add

        # Treat very common tokens composed only of digits or single chars as
        # common/junk
        very_common_tids = set(old_tid for old_tid, token in enumerate(tokens_by_old_tid)
                          if token.isdigit() or len(token) == 1)
        junk_old_tids.update(very_common_tids)

        # TODO: Why??? Ensure that tokens that are only found in SPDX keys are
        # treated as common/junk
        if _spdx_token_ids:
            junk_old_tids.update(_spdx_token_ids)

        # TODO: ensure common number as words are treated as very common
        # (one, two, and first, second, etc.)?

        # TODO: add and treat person and place names as always being common/junk

        # Build the candidate common/junk set as an approximate proportion of
        # total tokens
        len_tokens = len(tokens_by_old_tid)
        junk_max = int(len_tokens * PROPORTION_OF_JUNK)

        # Use a curated list of common tokens sorted by decreasing frequency as
        # the basis to determine common/junk status.
        old_dictionary_get = old_dictionary.get
        for token in _ranked_tokens():
            # stop when we reach the maximum common/junk proportion
            if len(junk_old_tids) == junk_max:
                break
            old_tid = old_dictionary_get(token)
            if old_tid is not None and old_tid not in never_junk_old_tids:
                junk_old_tids_add(old_tid)

        len_junk = len(junk_old_tids)

        # Assemble our final set of good old token id
        range_len_tokens = list(range(len_tokens))
        good_old_tids = set(range_len_tokens) - junk_old_tids
        assert len_tokens == len(junk_old_tids) + len(good_old_tids)

        # Sort the list of old token ids: junk before good, then by decreasing
        # frequencies, then old id.
        # This sort does the renumbering proper of old to new token ids
        key = lambda i: (i in good_old_tids, -tokens_count_by_old_tid[i], i)
        new_to_old_tids = sorted(range_len_tokens, key=key)

        enum_new_to_old_tids = list(enumerate(new_to_old_tids))

        # keep a mapping from old to new id used for renumbering index structures
        old_to_new_tids = [new_tid
           for new_tid, _old_tid in sorted(enum_new_to_old_tids, key=itemgetter(1))]

        # create the new ids -> tokens string mapping
        tokens_by_new_tid = [tokens_by_old_tid[old_tid]
             for _new_tid, old_tid in enum_new_to_old_tids]

        # create the new dictionary tokens trings -> new id
        new_dictionary = {token: new_tid
            for new_tid, token in enumerate(tokens_by_new_tid)}
        # OPTIMIZED
        sparsify(new_dictionary)

        old_tids_by_rid = self.tids_by_rid

        # mapping of rule_id->new token_ids array
        new_tids_by_rid = [array('h', (old_to_new_tids[tid] for tid in old_tids))
            for old_tids in old_tids_by_rid]

        # Now do a few sanity checks...
        # By construction this should always be true
        assert set(tokens_by_new_tid) == set(tokens_by_old_tid)

        # TODO: Check that the junk count choice is correct: for instance using
        # some stats based on standard deviation or markov chains or similar
        # conditional probabilities such that we verify that we CANNOT create a
        # distinctive meaningful license string made entirely from junk tokens

        return len_junk, new_dictionary, tokens_by_new_tid, new_tids_by_rid


def get_weak_rids(len_junk, tids_by_rid, _idx):
    """
    Return a set of "weak" rule ids made entirely of junk tokens: they can only be
    matched using an automaton.
    """
    weak_rids = set()
    weak_rids_add = weak_rids.add
    for rid, tids in enumerate(tids_by_rid):
        if any(t >= len_junk for t in tids):
            continue
        weak_rids_add(rid)

    if TRACE :
        for rid in sorted(weak_rids):
            rule = _idx.rules_by_rid[rid]
            if not rule.is_negative:
                message = (
                    'WARNING: Weak rule, made only of frequent junk tokens. '
                    'Can only be matched exactly:',
                    _idx.rules_by_rid[rid].identifier,
                    u' '.join(_idx.tokens_by_tid[t] for t in tids))
                logger_debug(u' '.join(message))

    return weak_rids


def get_matched_rule_ids(matches, query_run):
    """
    Yield the subset of matched rule ids from a `matches` LicenseMatch
    sequence that are within the `query_run` query run.
    """
    qstart = query_run.start
    qend = query_run.end

    for match in matches:
        if qstart <= match.qstart and match.qend <= qend:
            yield match.rule.rid
