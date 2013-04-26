#! /usr/bin/python -tt
# surrogateyum.py - Surrogate yum checks updates for other hosts
#
# Copyright (C) 2013 Satoru SATOH <ssato@redhat.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
from logging import DEBUG, INFO

import datetime
import logging
import optparse
import os
import os.path
import re
import shutil
import shlex
import subprocess
import sys


_CURDIR = os.path.curdir
_TODAY = datetime.datetime.now().strftime("%Y%m%d")
_WORKDIR = os.path.join(_CURDIR, "surrogate-yum-root-" + _TODAY)

_DEFAULTS = dict(path=None, root=_WORKDIR, dist="auto", format=False,
                 force=False, verbose=False)
_ARGV_SEP = "--"


def run(cmd):
    """
    :param cmd: Command string
    :return: (output :: str ,err_output :: str, exitcode :: Int)
    """
    logging.debug("cmd: " + cmd)
    p = subprocess.Popen(cmd, shell=True, stderr=subprocess.PIPE,
                         stdout=subprocess.PIPE)
    (out, err) = p.communicate()
    return (out, err, p.returncode)


def setup(path, root, force=False):
    """
    :param path: Path to the 'Packages' rpm database originally came from
                 /var/lib/rpm on the target host.
    :param root: The temporal root directry to put the rpm database.
    :param force: Force overwrite the rpmdb file previously copied.
    """
    assert root != "/"

    rpmdb_path = os.path.join(root, "var/lib/rpm")
    rpmdb_Packages_path = os.path.join(rpmdb_path, "Packages")

    if not os.path.exists(rpmdb_path):
        logging.debug("Creating rpmdb dir: " + rpmdb_path)
        os.makedirs(rpmdb_path)

    if os.path.exists(rpmdb_Packages_path) and not force:
        raise RuntimeError("RPM DB already exists: " + rpmdb_Packages_path)
    else:
        logging.debug("Copying RPM DB: %s -> %s/" % (path, rpmdb_path))
        shutil.copy2(path, rpmdb_Packages_path)


def detect_dist():
    if os.path.exists("/etc/fedora-release"):
        return "fedora"
    elif os.path.exists("/etc/redhat-release"):
        return "rhel"
    else:
        return "uknown"


def surrogate_operation(root, operation):
    """
    Surrogates yum operation (command).

    :param root: Pivot root dir where var/lib/rpm/Packages of the target host
                 exists, e.g. /root/host_a/
    :param operation: Yum operation (command), e.g. 'list-sec'
    """
    c = "yum --installroot=%s %s" % (os.path.abspath(root), operation)
    return run(c)


def _is_errata_line(line, dist):
    if dist == "fedora":
        reg = re.compile(r"^FEDORA-")
    else:  # RHEL:
        reg = re.compile(r"^RH[SBE]A-")

    return line and reg.match(line)


def result_fail(cmd, result):
    #logging.debug("result=(%s, %s, %d)" % result)
    raise RuntimeError(
        "Could not get the result. op=" + cmd + \
        ", out=%s, err=%s, rc=%d" % result
    )


def list_errata_g(root, dist):
    """
    A generator to return errata found in the output result of 'yum list-sec'
    one by one.

    :param root: Pivot root dir where var/lib/rpm/Packages of the target host
                 exists, e.g. /root/host_a/
    :param dist: Distribution name
    """
    result = surrogate_operation(root, "list-sec")
    if result[-1] == 0:
        for line in result[0].splitlines():
            if _is_errata_line(line, dist):
                yield line
            #else:
            #    yield "Not matched: " + line
    else:
        result_fail("list-sec", result)


def list_updates_g(root, *args):
    """
    FIXME: Ugly and maybe yum-version-dependent implementation.

    A generator to return updates found in the output result of 'yum
    check-update' one by one.

    :param root: Pivot root dir where var/lib/rpm/Packages of the target host
                 exists, e.g. /root/host_a/
    """
    # NOTE: 'yum check-update' looks returns !0 exit code (e.g. 100) when there
    # are any updates found.
    result = surrogate_operation(root, "check-update")
    if result[0]:
        # It seems that yum prints out an empty line before listing updates.
        in_list = False
        for line in result[0].splitlines():
            if line:
                if in_list:
                    yield line
            else:
                in_list = True
    else:
        result_fail("check-update", result)


def get_errata_deails(errata):
    """
    TBD

    :param errata: Errata advisory
    """
    return None


def run_yum_cmd(root, yum_args, *args):
    result = surrogate_operation(root, yum_args)
    if result[-1] == 0:
        print result[0]
    else:
        # FIXME: Ugly code based on heuristics.
        if "check-update" in yum_args:
            print result[0]
        else:
            result_fail(yum_args, result)


_FORMATABLE_COMMANDS = {"check-update": list_updates_g,
                        "list-sec": list_errata_g, }


def option_parser(defaults=_DEFAULTS, sep=_ARGV_SEP, fmt_cmds=_FORMATABLE_COMMANDS):
    p = optparse.OptionParser(
        "%%prog [OPTION ...] %s yum_command_and_options..." % sep
    )
    p.set_defaults(**defaults)

    p.add_option("-p", "--path", help="Path to the rpmdb (/var/lib/rpm/Packages)")
    p.add_option("-r", "--root", help="Output root dir [%default]")
    p.add_option("-d", "--dist", choices=("rhel", "fedora", "auto"),
                 help="Select distribution [%default]")
    p.add_option("-F", "--format", action="store_true",
                 help="Format outputs of some commands ("
                       ", ".join(fmt_cmds.keys()) + ") [%default]")
    p.add_option("-f", "--force", action="store_true",
                 help="Force overwrite pivot rpmdb and outputs even if exists")
    p.add_option("-v", "--verbose", action="store_true", help="Verbose mode")

    return p


def split_yum_args(argv, sep=_ARGV_SEP):
    sep_idx = argv.index("--")
    return (argv[:sep_idx], argv[sep_idx+1:])


def main(argv=sys.argv[1:], sep=_ARGV_SEP, fmtble_cmds=_FORMATABLE_COMMANDS):
    p = option_parser()

    if sep not in argv:
        logging.error("No yum command and options specified after '--'")
        p.print_usage()

    (self_argv, yum_argv) = split_yum_args(argv)

    if not yum_argv:
        p.print_usage()
        sys.exit(-1)

    (options, args) = p.parse_args(self_argv)

    logging.getLogger().setLevel(DEBUG if options.verbose else INFO)

    if options.dist == "auto":
        options.dist = detect_dist()

    if not options.path:
        options.path = raw_input("Path to the rpm db to surrogate > ")

    setup(options.path, options.root, options.force)

    if options.format:
        f = None
        for c in fmtble_cmds.keys():
            if c in yum_argv:
                f = fmtble_cmds[c]
                logging.debug("cmd=%s, fun=%s" % (c, f))
                break

        if f is None:
            run_yum_cmd(options.root, ' '.join(yum_argv))
        else:
            for x in f(options.root, options.dist):
                sys.stdout.write(str(x) + "\n")
    else:
        run_yum_cmd(options.root, ' '.join(yum_argv))


if __name__ == '__main__':
    main()

# vim:sw=4:ts=4:et:
