#
# -*- coding: utf-8 -*-
#
# main module of rpmkit.updateinfo
#
# Copyright (C) 2013 Satoru SATOH <ssato@redhat.com>
# Copyright (C) 2013, 2014 Red Hat, Inc.
#
# License: GPLv3+
#
from rpmkit.globals import _
from operator import itemgetter

import rpmkit.updateinfo.yumwrapper
import rpmkit.updateinfo.yumbase
import rpmkit.updateinfo.dnfbase
import rpmkit.updateinfo.utils
import rpmkit.memoize
import rpmkit.rpmutils
import rpmkit.utils as U
import rpmkit.swapi

# It looks available in EPEL for RHELs:
#   https://apps.fedoraproject.org/packages/python-bunch
import bunch
import calendar
import datetime
import itertools
import logging
import operator
import os
import os.path
import re
import tablib


LOG = logging.getLogger("rpmkit.updateinfo")

_RPM_LIST_FILE = "packages.json"
_ERRATA_LIST_FILE = "errata.json"
_UPDATES_LIST_FILE = "updates.json"

BACKENDS = dict(yumwrapper=rpmkit.updateinfo.yumwrapper.Base,
                yumbase=rpmkit.updateinfo.yumbase.Base,
                dnfbase=rpmkit.updateinfo.dnfbase.Base)
DEFAULT_BACKEND = BACKENDS["yumbase"]

DEFAULT_CVSS_SCORE = 4.0
ERRATA_KEYWORDS = ["crash", "panic", "hang", "SEGV", "segmentation fault"]
CORE_RPMS = ["kernel", "glibc", "bash", "openssl", "zlib"]

rpmkit.updateinfo.yumwrapper.LOG.setLevel(logging.WARN)
rpmkit.updateinfo.yumbase.LOG.setLevel(logging.WARN)
rpmkit.updateinfo.dnfbase.LOG.setLevel(logging.WARN)


def rpm_list_path(workdir, filename=_RPM_LIST_FILE):
    """
    :param workdir: Working dir to dump the result
    :param filename: Output file basename
    """
    return os.path.join(workdir, filename)


def errata_list_path(workdir, filename=_ERRATA_LIST_FILE):
    """
    :param workdir: Working dir to dump the result
    :param filename: Output file basename
    """
    return os.path.join(workdir, filename)


def updates_file_path(workdir, filename=_UPDATES_LIST_FILE):
    """
    :param workdir: Working dir to dump the result
    """
    return os.path.join(workdir, filename)


def mk_cve_vs_cvss_map():
    """
    Make up CVE vs. CVSS map w/ using swapi's virtual APIs.

    :return: A list of CVE details :: {cve: {cve, url, score, metrics}, }
    """
    return dict((c["cve"], c) for c in
                rpmkit.swapi.call("swapi.cve.getAll") if c)


def fetch_cve_details(cve, cve_cvss_map={}):
    """
    :param cve: A dict represents CVE :: {id:, url:, ...}
    :param cve_cvss_map: A dict :: {cve: cve_and_cvss_data}

    :return: A dict represents CVE and its CVSS metrics
    """
    cveid = cve.get("id", cve.get("cve"))
    dcve = cve_cvss_map.get(cveid)
    if dcve:
        cve.update(**dcve)
        return cve

    try:
        dcve = rpmkit.swapi.call("swapi.cve.getCvss", [cveid])
        if dcve:
            dcve = dcve[0]  # :: dict
            dcve["nvd_url"] = dcve["url"]
            dcve["url"] = cve["url"]
            cve.update(**dcve)

    except Exception as e:
        LOG.warn(_("Could not fetch CVSS metrics of %s, err=%s"),
                 cveid, str(e))
        dcve = dict(cve=cveid, )

    return cve


def _fmt_cve(cve):
    if 'score' in cve:
        return '%(cve)s (score=%(score)s, metrics=%(metrics)s, url=%(url)s)'
    else:
        return '%(cve)s (CVSS=N/A)'


def _fmt_cvess(cves):
    """
    :param cves: List of CVE dict {cve, score, url, metrics} or str "cve".
    :return: List of CVE strings
    """
    try:
        cves = [_fmt_cve(c) % c for c in cves]
    except KeyError:
        pass
    except Exception as e:
        raise RuntimeError("Wrong CVEs: %s, exc=%s" % (str(cves), str(e)))

    return cves


def _fmt_bzs(bzs, summary=False):
    """
    :param cves: List of CVE dict {cve, score, url, metrics} or str "cve".
    :return: List of CVE strings
    """
    def _fmt(bz):
        if summary and "summary" in bz:
            return "bz#%(id)s: %(summary)s (%(url)s"
        else:
            return "bz#%(id)s (%(url)s)"

    try:
        bzs = [_fmt(bz) % bz for bz in bzs]
    except KeyError:
        LOG.warn(_("BZ Key error: %s"), str(bzs))
        pass

    return bzs


def _make_cell_data(x, key, default="N/A"):
    if key == "cves":
        cves = x.get("cves", [])
        try:
            return ", ".join(_fmt_cvess(cves)) if cves else default
        except Exception as e:
            raise RuntimeError("Wrong CVEs: %s, exc=%s" % (str(cves), str(e)))

    elif key == "bzs":
        bzs = x.get("bzs", [])
        return ", ".join(_fmt_bzs(bzs)) if bzs else default

    else:
        v = x.get(key, default)
        return ", ".join(v) if isinstance(v, (list, tuple)) else v


def make_dataset(list_data, title=None, headers=[], lheaders=[]):
    """
    :param list_data: List of data
    :param title: Dataset title to be used as worksheet's name
    :param headers: Dataset headers to be used as column headers, etc.
    :param lheaders: Localized version of `headers`
    """
    dataset = tablib.Dataset()

    # TODO: Check title as valid worksheet name, ex. len(title) <= 31.
    # See also xlwt.Utils.valid_sheet_name.
    if title:
        dataset.title = title

    if headers:
        if lheaders:
            dataset.headers = [h.replace('_s', '') for h in lheaders]
        else:
            dataset.headers = [h.replace('_s', '') for h in headers]

        for x in list_data:
            dataset.append([_make_cell_data(x, h) for h in headers])
    else:
        for x in list_data:
            dataset.append(x.values())

    return dataset


def errata_date(date_s):
    """
    NOTE: Errata issue_date and update_date format: month/day/year,
        e.g. 12/16/10.

    >>> errata_date("12/16/10")
    (2010, 12, 16)
    >>> errata_date("2014-10-14 00:00:00")
    (2014, 10, 14)
    """
    if '-' in date_s:
        (y, m, d) = date_s.split()[0].split('-')
        return (int(y), int(m), int(d))
    else:
        (m, d, y) = date_s.split('/')
        return (int("20" + y), int(m), int(d))


def cve_socre_ge(cve, score=DEFAULT_CVSS_SCORE, default=False):
    """
    :param cve: A dict contains CVE and CVSS info.
    :param score: Lowest score to select CVEs (float). It's Set to 4.0 (PCIDSS
        limit) by default:

        * NVD Vulnerability Severity Ratings: http://nvd.nist.gov/cvss.cfm
        * PCIDSS: https://www.pcisecuritystandards.org

    :param default: Default value if failed to get CVSS score to compare with
        given score

    :return: True if given CVE's socre is greater or equal to given score.
    """
    if "score" not in cve:
        LOG.warn(_("CVE %(cve)s does not have CVSS base metrics and score"),
                 cve)
        return default

    try:
        return float(cve["score"]) >= float(score)
    except Exception:
        LOG.warn(_("Failed to compare CVE's score: %s, score=%.1f"),
                 str(cve), score)

    return default


def p2na(pkg):
    """
    :param pkg: A dict represents package info including N, E, V, R, A
    """
    return (pkg["name"], pkg["arch"])


def sgroupby(xs, kf, kf2=None):
    """
    :param xs: Iterable object, e.g. a list, a tuple, etc.
    :param kf: Key function to sort `xs` and group it
    :param kf2: Key function to sort each group in result

    :return: A generator to yield items in `xs` grouped by `kf`
    """
    return (list(g) if kf2 is None else sorted(g, key=kf2) for _k, g
            in itertools.groupby(sorted(xs, key=kf), kf))


def list_updates_from_errata(errata):
    """
    :param errata: A list of errata dict
    """
    us = sorted(U.uconcat(e.get("updates", []) for e in errata),
                key=itemgetter("name"))
    return [sorted(g, cmp=rpmkit.rpmutils.pcmp, reverse=True)[0]
            for g in sgroupby(us, itemgetter("name"))]


def list_latest_errata_groupby_updates(es):
    """
    :param es: A list of errata dict
    :return: A list of items in `es` grouped by update names
    """
    ung = lambda e: sorted(set(u["name"] for u in e.get("updates", [])))
    return [xs[-1] for xs in sgroupby(es, ung, itemgetter("issue_date"))]


def compute_delta(refdir, errata, updates):
    """
    :param refdir: Dir has reference data files: packages.json, errata.json
        and updates.json
    :param errata: A list of errata
    :param updates: A list of update packages
    """
    emsg = "Reference %s not found: %s"
    assert os.path.exists(refdir), emsg % ("data dir", refdir)

    ref_es_file = os.path.join(refdir, "errata.json")
    ref_us_file = os.path.join(refdir, "updates.json")
    assert os.path.exists(ref_es_file), emsg % ("errata file", ref_es_file)
    assert os.path.exists(ref_us_file), emsg % ("updates file", ref_us_file)

    ref_es_data = U.json_load(ref_es_file)
    ref_us_data = U.json_load(ref_us_file)
    LOG.debug(_("Loaded reference errata and updates file"))

    nevra_keys = ("name", "epoch", "version", "release", "arch")
    ref_eadvs = U.uniq(e["advisory"] for e in ref_es_data["data"])
    ref_nevras = U.uniq([p[k] for k in nevra_keys] for p in
                        ref_us_data["data"])

    return ([e for e in errata if e["advisory"] in ref_eadvs],
            [u for u in updates if [u[k] for k in nevra_keys]
             in ref_nevras])


def errata_matches_keywords_g(errata, keywords=ERRATA_KEYWORDS):
    """
    :param errata: A list of errata
    :param keywords: Keyword list to filter 'important' RHBAs

    :return: A generator to yield errata of which description contains any of
        given keywords
    """
    for e in errata:
        mks = [k for k in keywords if k in e["description"]]
        if mks:
            e["keywords"] = mks
            yield e


def _ekeyfunc(e):
    return (len(e["update_names"]), itemgetter("issue_date"))


def errata_of_rpms(es, rpms=[], keyfunc=_ekeyfunc):
    """
    :param es: A list of errata
    :param rpms: RPM names to select relevant errata

    :return: A list of errata relevant to any of given RPMs
    """
    return sorted((e for e in es if any(n in e["update_names"] for n in rpms)),
                  key=keyfunc, reverse=True)


def higher_score_cve_errata_g(errata, score=DEFAULT_CVSS_SCORE):
    """
    :param errata: A list of errata
    :param score: CVSS base metrics score
    """
    for e in errata:
        # NOTE: Skip older CVEs do not have CVSS base metrics and score.
        cves = [c for c in e.get("cves", []) if "score" in c]
        if cves and any(cve_socre_ge(cve, score) for cve in cves):
            cvsses_s = ", ".join("{cve} ({score}, {metrics})".format(**c)
                                 for c in cves)
            cves_s = ", ".join("{cve} ({url})".format(**c) for c in cves)
            e["cvsses_s"] = cvsses_s
            e["cves_s"] = cves_s

            yield e


def errata_complement_g(errata, updates, score):
    """
    TODO: What should be complemented?

    :param errata: A list of errata
    :param updates: A list of update packages
    :param score: CVSS score
    """
    unas = set(p2na(u) for u in updates)
    for e in errata:
        e["updates"] = U.uniq(p for p in e.get("packages", []) if p2na(p)
                              in unas)
        e["update_names"] = U.uniq(u["name"] for u in e["updates"])

        if score > 0:
            e["cves"] = [fetch_cve_details(cve) for cve in e.get("cves", [])]

        yield e


_DATE_REG = re.compile(r"^(\d{4})(?:.(\d{2})(?:.(\d{2}))?)?$")


def round_ymd(year, mon, day, roundout=False):
    """
    :param roundout: Round out given date to next year, month, day if this
        parameter is True

    >>> round_ymd(2014, None, None, True)
    (2015, 1, 1)
    >>> round_ymd(2014, 11, None, True)
    (2014, 12, 1)
    >>> round_ymd(2014, 12, 24, True)
    (2014, 12, 25)
    >>> round_ymd(2014, 12, 31, True)
    (2015, 1, 1)
    >>> round_ymd(2014, None, None)
    (2014, 1, 1)
    >>> round_ymd(2014, 11, None)
    (2014, 11, 1)
    >>> round_ymd(2014, 12, 24)
    (2014, 12, 24)
    """
    if mon is None:
        return (year + 1 if roundout else year, 1, 1)

    elif day is None:
        if roundout:
            return (year + 1, 1, 1) if mon == 12 else (year, mon + 1, 1)
        else:
            return (year, mon, 1)
    else:
        if roundout:
            last_day = calendar.monthrange(year, mon)[1]
            if day == last_day:
                return (year + 1, 1, 1) if mon == 12 else (year, mon + 1, 1)
            else:
                return (year, mon, day + 1)
        else:
            return (year, mon, day)


def ymd_to_date(ymd, roundout=False, datereg=_DATE_REG):
    """
    :param ymd: Date string in YYYY[-MM[-DD]]
    :param roundout: Round out to next year, month if True and day was not
        given; ex. '2014' -> (2015, 1, 1), '2014-11' -> (2014, 12, 1)
        '2014-12-24' -> (2014, 12, 25), '2014-12-31' -> (2015, 1, 1) if True
        and '2014' -> (2014, 1, 1), '2014-11' -> (2014, 11, 1) if False.
    :param datereg: Date string regex

    :return: A tuple of (year, month, day) :: (int, int, int)

    >>> ymd_to_date('2014-12-24')
    (2014, 12, 24)
    >>> ymd_to_date('2014-12')
    (2014, 12, 1)
    >>> ymd_to_date('2014')
    (2014, 1, 1)
    >>> ymd_to_date('2014-12-24', True)
    (2014, 12, 25)
    >>> ymd_to_date('2014-12-31', True)
    (2015, 1, 1)
    >>> ymd_to_date('2014-12', True)
    (2015, 1, 1)
    >>> ymd_to_date('2014', True)
    (2015, 1, 1)
    """
    m = datereg.match(ymd)
    if not m:
        LOG.error("Invalid input for normalize_date(): %s", ymd)

    d = m.groups()
    int_ = lambda x: x if x is None else int(x)

    return round_ymd(int(d[0]), int_(d[1]), int_(d[2]), roundout)


def analyze_errata(errata, updates, score=-1, keywords=ERRATA_KEYWORDS,
                   core_rpms=CORE_RPMS, period=()):
    """
    :param errata: A list of applicable errata sorted by severity
        if it's RHSA and advisory in ascending sequence
    :param updates: A list of update packages
    :param score: CVSS base metrics score
    :param keywords: Keyword list to filter 'important' RHBAs
    :param core_rpms: Core RPMs to filter errata by them
    :param period: Period of errata in format of YYYY[-MM[-DD]],
        ex. ("2014-10-01", "2014-11-01")
    """
    rhsa = [e for e in errata if e.get("severity", None) is not None]
    rhsa_cri = [e for e in rhsa if e.get("severity") == "Critical"]
    rhsa_imp = [e for e in rhsa if e.get("severity") == "Important"]
    rhsa_cri_latests = list_latest_errata_groupby_updates(rhsa_cri)
    rhsa_imp_latests = list_latest_errata_groupby_updates(rhsa_imp)

    us_of_rhsa_cri = list_updates_from_errata(rhsa_cri)
    us_of_rhsa_imp = list_updates_from_errata(rhsa_imp)

    rhba = [e for e in errata if e["advisory"][2] == 'B']

    kf = lambda e: (len(e.get("keywords", [])), e["issue_date"],
                    e["update_names"])
    rhba_by_kwds = sorted(errata_matches_keywords_g(rhba, keywords),
                          key=kf, reverse=True)
    rhba_of_rpms_by_kwds = errata_of_rpms(rhba_by_kwds, core_rpms, kf)
    rhba_of_rpms = errata_of_rpms(rhba, core_rpms)

    if score > 0:
        rhsa_by_score = list(higher_score_cve_errata_g(rhsa, score))
        rhba_by_score = list(higher_score_cve_errata_g(rhba, score))
        us_of_rhsa_by_score = list_updates_from_errata(rhsa_by_score)
        us_of_rhba_by_score = list_updates_from_errata(rhba_by_score)
    else:
        rhsa_by_score = []
        rhba_by_score = []
        us_of_rhsa_by_score = []
        us_of_rhba_by_score = []

    us_of_rhba_by_kwds = list_updates_from_errata(rhba_by_kwds)

    rhea = [e for e in errata if e["advisory"].startswith("RHEA")]

    return dict(rhsa=rhsa, rhsa_cri=rhsa_cri, rhsa_imp=rhsa_imp,
                rhsa_cri_latests=rhsa_cri_latests,
                rhsa_imp_latests=rhsa_imp_latests,
                rhsa_by_cvss_score=rhsa_by_score,
                us_of_rhsa_cri=us_of_rhsa_cri,
                us_of_rhsa_imp=us_of_rhsa_imp,
                rhba=rhba, rhba_by_kwds=rhba_by_kwds,
                rhba_of_core_rpms=rhba_of_rpms,
                rhba_of_core_rpms_by_kwds=rhba_of_rpms_by_kwds,
                rhba_by_cvss_score=rhba_by_score,
                us_of_rhba_by_kwds=us_of_rhba_by_kwds,
                us_of_rhsa_by_cvss_score=us_of_rhsa_by_score,
                us_of_rhba_by_cvss_score=us_of_rhba_by_score,
                rhea=rhea)


def padding_row(row, mcols):
    """
    :param rows: A list of row data :: [[]]

    >>> padding_row(['a', 1], 3)
    ['a', 1, '']
    >>> padding_row([], 2)
    ['', '']
    """
    return row + [''] * (mcols - len(row))


def make_overview_dataset(workdir, data, score=-1, keywords=ERRATA_KEYWORDS):
    """
    :param workdir: Working dir to dump the result
    :param data: RPMs, Update RPMs and various errata data summarized
    :param score: CVSS base metrics score limit

    :return: An instance of tablib.Dataset becomes a worksheet represents the
        overview of analysys reuslts
    """
    rows = [[_("Critical or Important RHSAs (Security Errata)")],
            [_("# of Critical RHSAs"), len(data["errata"]["rhsa_cri"])],
            [_("# of Critical RHSAs (latests only)"),
             len(data["errata"]["rhsa_cri_latests"])],
            [_("# of Important RHSAs"), len(data["errata"]["rhsa_imp"])],
            [_("# of Important RHSAs (latests only)"),
             len(data["errata"]["rhsa_imp_latests"])],
            [_("Update RPMs by Critical or Important RHSAs at minimum")],
            [_("# of Update RPMs by Critical RHSAs at minimum"),
             len(data["errata"]["us_of_rhsa_cri"])],
            [_("# of Update RPMs by Important RHSAs at minimum"),
             len(data["errata"]["us_of_rhsa_imp"])],
            [],
            [_("RHBAs (Bug Errata) by keywords: %s") % ", ".join(keywords)],
            [_("# of RHBAs by keywords"), len(data["errata"]["rhba_by_kwds"])],
            [_("# of Update RPMs by RHBAs by keywords at minimum"),
             len(data["errata"]["us_of_rhba_by_kwds"])]]

    if score > 0:
        rows += [[],
                 [_("RHSAs and RHBAs by CVSS score")],
                 [_("# of RHSAs of CVSS Score >= %.1f") % score,
                  len(data["errata"]["rhsa_by_cvss_score"])],
                 [_("# of Update RPMs by the above RHSAs at minimum"),
                  len(data["errata"]["us_of_rhsa_by_cvss_score"])],
                 [_("# of RHBAs of CVSS Score >= %.1f") % score,
                  len(data["errata"]["rhba_by_cvss_score"])],
                 [_("# of Update RPMs by the above RHBAs at minimum"),
                  len(data["errata"]["us_of_rhba_by_cvss_score"])]]

    rows += [[],
             [_("# of RHSAs"), len(data["errata"]["rhsa"])],
             [_("# of RHBAs"), len(data["errata"]["rhba"])],
             [_("# of RHEAs (Enhancement Errata)"),
              len(data["errata"]["rhea"])],
             [_("# of Update RPMs"), len(data["updates"])],
             [_("# of Installed RPMs"), len(data["installed"])]]

    headers = (_("Item"), _("Value"), _("Notes"))
    dataset = tablib.Dataset(headers=headers)
    dataset.title = _("Overview of analysis results")

    mcols = len(headers)
    for row in rows:
        try:
            if row and len(row) == 1:  # Special case: separator
                dataset.append_separator(row[0])
            else:
                dataset.append(padding_row(row, mcols))
        except:
            LOG.error("row=" + str(row))
            raise

    return dataset


def dump_xls(dataset, filepath):
    book = tablib.Databook(dataset)
    with open(filepath, 'wb') as out:
        out.write(book.xls)


def dump_results(workdir, rpms, errata, updates, score=-1,
                 keywords=ERRATA_KEYWORDS, core_rpms=[], details=True):
    """
    :param workdir: Working dir to dump the result
    :param rpms: A list of installed RPMs
    :param errata: A list of applicable errata
    :param updates: A list of update RPMs
    :param score: CVSS base metrics score
    :param keywords: Keyword list to filter 'important' RHBAs
    :param core_rpms: Core RPMs to filter errata by them
    :param details: Dump details also if True
    """
    data = dict(errata=analyze_errata(errata, updates, score, keywords,
                                      core_rpms),
                rpms=rpms, installed=rpms, updates=updates,
                rpmnames_need_updates=U.uniq(u["name"] for u in updates))
    U.json_dump(data, os.path.join(workdir, "summary.json"))

    # FIXME: How to keep DRY principle?
    rpmkeys = ["name", "version", "release", "epoch", "arch"]
    lrpmkeys = [_("name"), _("version"), _("release"), _("epoch"), _("arch")]

    rpmdkeys = rpmkeys + ["summary", "vendor", "buildhost"]
    lrpmdkeys = lrpmkeys + [_("summary"), _("vendor"), _("buildhost")]

    sekeys = ("advisory", "severity", "synopsis", "url", "update_names")
    lsekeys = (_("advisory"), _("severity"), _("synopsis"), _("url"),
               _("update_names"))
    bekeys = ("advisory", "keywords", "synopsis", "url", "update_names")
    lbekeys = (_("advisory"), _("keywords"), _("synopsis"), _("url"),
               _("update_names"))

    ds = [make_overview_dataset(workdir, data, score, keywords),
          make_dataset((data["errata"]["rhsa_cri_latests"] +
                        data["errata"]["rhsa_imp_latests"]),
                       _("Cri-Important RHSAs (latests)"), sekeys, lsekeys),
          make_dataset(data["errata"]["rhsa_cri"] + data["errata"]["rhsa_imp"],
                       _("Critical or Important RHSAs"), sekeys, lsekeys),
          make_dataset(data["errata"]["us_of_rhsa_cri"],
                       _("Update RPMs by RHSAs (Critical)"), rpmkeys,
                       lrpmkeys),
          make_dataset(data["errata"]["us_of_rhsa_imp"],
                       _("Updates by RHSAs (Important)"), rpmkeys, lrpmkeys),
          make_dataset(data["errata"]["rhba_by_kwds"], _("RHBAs (keyword)"),
                       bekeys, lbekeys),
          make_dataset(data["errata"]["us_of_rhba_by_kwds"],
                       _("Updates by RHBAs (Keyword)"), rpmkeys, lrpmkeys),
          make_dataset(data["errata"]["rhba_of_core_rpms_by_kwds"],
                       _("RHBAs (core rpms, keywords)"), bekeys, lbekeys),
          make_dataset(data["errata"]["rhba_of_core_rpms"],
                       _("RHBAs (core rpms)"), bekeys, lbekeys)]

    if score >= 0:
        cvss_ds = [make_dataset(data["errata"]["rhsa_by_cvss_score"],
                                _("RHSAs (CVSS score >= %.1f)") % score,
                                ("advisory", "severity", "synopsis",
                                 "cves", "cvsses_s", "url"),
                                (_("advisory"), _("severity"), _("synopsis"),
                                 _("cves"), _("cvsses_s"), _("url"))),
                   make_dataset(data["errata"]["rhba_by_cvss_score"],
                                _("RHBAs (CVSS score >= %.1f)") % score,
                                ("advisory", "synopsis", "cves",
                                 "cvsses_s", "url"),
                                (_("advisory"), _("synopsis"), _("cves"),
                                 _("cvsses_s"), _("url")))]
        ds.extend(cvss_ds)

    dump_xls(ds, os.path.join(workdir, "errata_summary.xls"))

    if details:
        dds = [make_dataset(errata, _("Errata Details"),
                            ("advisory", "type", "severity", "synopsis",
                             "description", "issue_date", "update_date", "url",
                             "cves", "bzs", "update_names"),
                            (_("advisory"), _("type"), _("severity"),
                            _("synopsis"), _("description"), _("issue_date"),
                            _("update_date"), _("url"), _("cves"),
                            _("bzs"), _("update_names"))),
               make_dataset(updates, _("Update RPMs"), rpmkeys, lrpmkeys),
               make_dataset(rpms, _("Installed RPMs"), rpmdkeys, lrpmdkeys)]

        dump_xls(dds, os.path.join(workdir, "errata_details.xls"))


def get_backend(backend, fallback=rpmkit.updateinfo.yumbase.Base,
                backends=BACKENDS):
    return backends.get(backend, fallback)


def prepare(root, workdir=None, repos=[], did=None,
            backend=DEFAULT_BACKEND, backends=BACKENDS):
    """
    :param root: Root dir of RPM db, ex. / (/var/lib/rpm)
    :param workdir: Working dir to save results
    :param repos: List of yum repos to get updateinfo data (errata and updtes)
    :param did: Identity of the data (ex. hostname) or empty str
    :param backend: Backend module to use to get updates and errata
    :param backends: Backend list

    :return: A bunch.Bunch object of (Base, workdir, installed_rpms_list)
    """
    root = os.path.abspath(root)  # Ensure it's absolute path.

    if workdir is None:
        LOG.info(_("Set workdir to root [%s]: %s"), did, root)
        workdir = root
    else:
        if not os.path.exists(workdir):
            LOG.debug(_("Creating working dir [%s]: %s"), did, workdir)
            os.makedirs(workdir)

    host = bunch.bunchify(dict(id=did, root=root, workdir=workdir,
                               repos=repos, available=False))

    if not rpmkit.updateinfo.utils.check_rpmdb_root(root):
        LOG.warn(_("RPM DB not available and analysis won't be done [%s]: %s"),
                 did, root)
        return host

    # pylint: disable=maybe-no-member
    base = get_backend(backend)(host.root, host.repos, workdir=host.workdir)
    LOG.debug(_("Initialized backend [%s]: %s"), host.id, base.name)
    host.base = base

    LOG.debug(_("Dump Installed RPMs list loaded from: %s [%s]"),
              host.root, host.id)
    host.installed = sorted(host.base.list_installed(),
                            key=operator.itemgetter("name", "epoch", "version",
                                                    "release"))
    LOG.info(_("%d Installed RPMs found [%s]"), len(host.installed), host.id)
    U.json_dump(dict(data=host.installed, ), rpm_list_path(host.workdir))
    host.available = True
    # pylint: enable=maybe-no-member

    return host


_TODAY = datetime.datetime.now().strftime("%Y-%m-%d")


def _d2i(d):
    """
    >>> _d2i((2014, 10, 1))
    20141001
    """
    return d[0] * 10000 + d[1] * 100 + d[2]


def period_to_dates(start_date, end_date=_TODAY):
    """
    :param period: Period of errata in format of YYYY[-MM[-DD]],
        ex. ("2014-10-01", "2014-11-01")

    >>> today = _d2i(ymd_to_date(_TODAY, True))
    >>> (20141001, today) == period_to_dates("2014-10-01")
    True
    >>> period_to_dates("2014-10-01", "2014-12-31")
    (20141001, 20150101)
    """
    return (_d2i(ymd_to_date(start_date)), _d2i(ymd_to_date(end_date, True)))


def errata_in_period(errata, start_date, end_date):
    """
    :param errata: A dict represents errata
    :param start_date, end_date: Start and end date of period,
        (year :: int, month :: int, day :: int)
    """
    d = _d2i(errata_date(errata["issue_date"]))

    return start_date <= d and d < end_date


def analyze(host, score=-1, keywords=ERRATA_KEYWORDS, core_rpms=[],
            period=(), refdir=None):
    """
    :param host: host object function :function:`prepare` returns
    :param score: CVSS base metrics score
    :param keywords: Keyword list to filter 'important' RHBAs
    :param core_rpms: Core RPMs to filter errata by them
    :param period: Period of errata in format of YYYY[-MM[-DD]],
        ex. ("2014-10-01", "2014-11-01")
    :param refdir: A dir holding reference data previously generated to
        compute delta (updates since that data)
    """
    base = host.base
    workdir = host.workdir

    timestamp = datetime.datetime.now().strftime("%F %T")
    metadata = bunch.bunchify(dict(id=host.id, root=host.root,
                                   workdir=host.workdir, repos=host.repos,
                                   backend=host.base.name, score=score,
                                   keywords=keywords,
                                   installed=len(host.installed),
                                   generated=timestamp))
    # pylint: disable=maybe-no-member
    LOG.debug(_("Dump metadata [%s]: root=%s"), metadata.id, metadata.root)
    # pylint: enable=maybe-no-member
    U.json_dump(metadata.toDict(), os.path.join(workdir, "metadata.json"))

    es = base.list_errata()
    LOG.info(_("%d Errata found for installed rpms [%s]"), len(es), host.id)

    us = base.list_updates()
    LOG.info(_("%d Update RPMs found for installed rpms [%s]"), len(us),
             host.id)

    us = U.uniq(us, key=itemgetter("name", "epoch", "version", "release"))
    es = U.uniq(errata_complement_g(es, us, score),
                cmp=rpmkit.updateinfo.utils.cmp_errata)

    host.errata = es
    host.updates = us

    LOG.debug(_("Dump Errata list..."))
    U.json_dump(dict(data=es, ), errata_list_path(workdir))

    LOG.debug(_("Dump Update RPMs list..."))
    U.json_dump(dict(data=us, ), updates_file_path(workdir))

    ips = host.installed

    LOG.info(_("Analyze and dump results of RPMs and errata data in %s..."),
             workdir)
    dump_results(workdir, ips, es, us, score, keywords, core_rpms)

    if period:
        (start_date, end_date) = period_to_dates(*period)
        period_s = "%s_%s" % (start_date, end_date)

        LOG.info(_("Analyze errata in period: %s ~ %s"), start_date, end_date)
        pes = [e for e in es if errata_in_period(e, start_date, end_date)]

        pdir = os.path.join(workdir, period_s)
        if not os.path.exists(pdir):
            LOG.debug(_("Creating period working dir [%s]: %s"), host.id, pdir)
            os.makedirs(pdir)

        dump_results(pdir, ips, pes, us, score, keywords, core_rpms, False)

    if refdir:
        LOG.debug(_("Computing delta errata and updates for data in %s"),
                  refdir)
        (es, us) = compute_delta(refdir, es, us)

        deltadir = os.path.join(workdir, "delta")
        if not os.path.exists(deltadir):
            LOG.debug(_("Creating delta working dir [%s]: %s"),
                      host.id, deltadir)
            os.makedirs(deltadir)

        LOG.info(_("%d Delta Errata found for installed rpms [%s]"),
                 len(es), host.id)
        U.json_dump(dict(data=es, ), errata_list_path(deltadir))

        LOG.info(_("%d Delta Update RPMs found for installed rpms [%s]"),
                 len(us), host.id)
        U.json_dump(dict(data=us, ), updates_file_path(deltadir))

        es = sorted(es, cmp=rpmkit.updateinfo.utils.cmp_errata)
        us = sorted(us, key=itemgetter("name", "epoch", "version", "release"))

        LOG.info(_("Dump analysis results of delta RPMs and errata data..."))
        dump_results(workdir, ips, es, us, score, keywords, core_rpms)


def main(root, workdir=None, repos=[], did=None, score=-1,
         keywords=ERRATA_KEYWORDS, rpms=CORE_RPMS, period=(),
         refdir=None, backend=DEFAULT_BACKEND, backends=BACKENDS):
    """
    :param root: Root dir of RPM db, ex. / (/var/lib/rpm)
    :param workdir: Working dir to save results
    :param repos: List of yum repos to get updateinfo data (errata and updtes)
    :param did: Identity of the data (ex. hostname) or empty str
    :param score: CVSS base metrics score
    :param keywords: Keyword list to filter 'important' RHBAs
    :param rpms: Core RPMs to filter errata by them
    :param period: Period of errata in format of YYYY[-MM[-DD]],
        ex. ("2014-10-01", "2014-11-01")
    :param refdir: A dir holding reference data previously generated to
        compute delta (updates since that data)
    :param backend: Backend module to use to get updates and errata
    :param backends: Backend list
    """
    host = prepare(root, workdir, repos, did, backend, backends)
    if host.available:
        analyze(host, score, keywords, rpms, period, refdir)

# vim:sw=4:ts=4:et:
