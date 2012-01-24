#
# myrepo.py - Manage your yum repo and RPMs:
#
#  * Setup your own yum repos
#  * build SRPMs and deploy SRPMs and RPMs into your repos.
#
# Copyright (C) 2011 Red Hat, Inc.
# Red Hat Author(s): Satoru SATOH <ssato@redhat.com>
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
# Requirements: createrepo, ssh, packagemaker (see below)
#
# SEE ALSO: createrepo(8)
# SEE ALSO: https://github.com/ssato/packagemaker
#
from Cheetah.Template import Template
from functools import reduce as foldl
from itertools import groupby, product

import ConfigParser as cp
import copy
import glob
import logging
import optparse
import os
import os.path
import platform
import pprint
import re
import rpm
import socket
import subprocess
import sys
import tempfile
import threading
import time


try:
    from collections import OrderedDict as dict

except ImportError:
    pass


WAIT_TYPE = (WAIT_FOREVER, WAIT_MIN, WAIT_MAX) = (None, "min", "max")
TIMEOUTS = (REMOTE_TIMEOUT, LOCAL_TIMEOUT, BUILD_TIMEOUT, MIN_TIMEOUT) = \
    (60 * 10, 60 * 5, 60 * 10, 5)  # [sec]

TEMPLATES = {
    "mock.cfg":
"""\
#for $k, $v in $cfg.iteritems()
#if "\\n" in $v
config_opts['$k'] = \"\"\"
$v
\"\"\"
#else
config_opts['$k'] = '$v'
#end if

#end for
""",
}


def memoize(fn):
    """memoization decorator.
    """
    cache = {}

    def wrapped(*args, **kwargs):
        key = repr(args) + repr(kwargs)
        if key not in cache:
            cache[key] = fn(*args, **kwargs)

        return cache[key]

    return wrapped


def compile_template(template, params, is_file=False):
    """
    TODO: Add test case that $template is a filename.

    >>> tmpl_s = "a=$a b=$b"
    >>> params = {'a':1, 'b':'b'}
    >>> assert "a=1 b=b" == compile_template(tmpl_s, params)
    """
    if is_file:
        tmpl = Template(file=template, searchList=params)
    else:
        tmpl = Template(source=template, searchList=params)

    return tmpl.respond()


@memoize
def list_archs(arch=None):
    """List 'normalized' architecutres this host (mock) can support.
    """
    default = ["x86_64", "i386"]   # This order should be kept.
    ia32_re = re.compile(r"i.86")  # i386, i686, etc.

    if arch is None:
        arch = platform.machine()

    if ia32_re.match(arch) is not None:
        return ["i386"]
    else:
        return default


@memoize
def list_dists():
    """List available dist names, e.g. ["fedora-14", "rhel-6"]
    """
    mockdir = "/etc/mock"
    arch = list_archs()[0]
    reg = re.compile("%s/(?P<dist>.+)-%s.cfg" % (mockdir, arch))

    return [
        reg.match(c).groups()[0] for c in \
            sorted(glob.glob("%s/*-%s.cfg" % (mockdir, arch)))
    ]


def is_local(fqdn_or_hostname):
    """
    >>> is_local("localhost")
    True
    >>> is_local("localhost.localdomain")
    True
    >>> is_local("repo-server.example.com")
    False
    >>> is_local("127.0.0.1")  # special case:
    False
    """
    return fqdn_or_hostname.startswith("localhost")


class ThreadedCommand(object):
    """
    Based on the idea found at
    http://stackoverflow.com/questions/1191374/subprocess-with-timeout
    """

    def __init__(self, cmd, user=None, host="localhost", workdir=os.curdir,
            stop_on_failure=True, timeout=None):
        self.host = host
        self.stop_on_failure = stop_on_failure
        self.user = get_username() if user is None else user
        self.timeout = timeout

        if is_local(host):
            if "~" in workdir:
                workdir = os.path.expanduser(workdir)
        else:
            cmd = "ssh -o ConnectTimeout=%d %s@%s 'cd %s && %s'" % \
                (MIN_TIMEOUT, user, host, workdir, cmd)
            workdir = os.curdir

        self.cmd = cmd
        self.workdir = workdir
        self.cmd_str = "%s [%s]" % (self.cmd, self.workdir)

        self.process = None
        self.thread = None
        self.result = None

    def run_async(self):
        def func():
            if logging.getLogger().level < logging.INFO:  # logging.DEBUG
                stdout = sys.stdout
            else:
                stdout = open("/dev/null", "w")

            #logging.info("Run: %s" % cmd_str_shorten)
            logging.info("Run: " + self.cmd_str)

            self.process = subprocess.Popen(self.cmd,
                                            bufsize=4096,
                                            shell=True,
                                            cwd=self.workdir,
                                            stdout=stdout,
                                            stderr=sys.stderr
            )
            self.result = self.process.wait()

            logging.debug("Finished: %s" % self.cmd_str)

        self.thread = threading.Thread(target=func)
        self.thread.start()

    def get_result(self):
        if self.thread is None:
            logging.warn(
                "Thread does not exist. Did you call %s.run_async() ?" % \
                    self.__class__.__name__
            )
            return None

        # it will block.
        self.thread.join(self.timeout)

        if self.thread.is_alive():
            logging.warn("Terminating: %s" % self.cmd_str)
            try:
                self.process.terminate()
            except OSError:  # the process exited already.
                pass

            self.thread.join()

        rc = self.result

        if rc != 0:
            emsg = "Failed: %s, rc=%d" % (self.cmd, rc)

            if self.stop_on_failure:
                raise RuntimeError(emsg)
            else:
                logging.warn(emsg)

        return rc

    def run(self):
        self.run_async()
        return self.get_result()


def run(cmd_str, user=None, host="localhost", workdir=os.curdir,
        stop_on_failure=True, timeout=None):
    cmd = ThreadedCommand(cmd_str, user, host, workdir, stop_on_failure, timeout)
    return cmd.run()


def prun_and_get_results(cmds, wait=WAIT_FOREVER):
    """
    @cmds  [ThreadedCommand]
    @wait  Int  Timewait value in seconds.
    """
    def is_valid_timeout(timeout):
        return timeout is None or isinstance(timeout, int) and timeout > 0

    for c in cmds:
        c.run_async()

    if wait != WAIT_FOREVER:
        ts = [c.timeout for c in cmds if is_valid_timeout(c.timeout)]

        if ts:
            if wait == WAIT_MAX:
                timeout = max(ts)
            elif wait == WAIT_MIN:
                timeout = min(ts)
            else:
                if not is_valid_timeout(wait):
                    RuntimeError(
                        "Invalid 'wait' value was passed to get_results: " + \
                            str(wait)
                    )
                else:
                    timeout = wait

            time.sleep(timeout)

    return [c.get_result() for c in cmds]


def snd(x, y):
    """
    >>> snd(1, 2)
    2
    """
    return y


@memoize
def is_git_available():
    return os.system("git --version > /dev/null 2> /dev/null") == 0


@memoize
def hostname():
    return socket.gethostname() or os.uname()[1]


@memoize
def get_username():
    """Get username.
    """
    return os.environ.get("USER", False) or os.getlogin()


@memoize
def get_email():
    if is_git_available():
        try:
            email = subprocess.check_output(
                "git config --get user.email 2>/dev/null", shell=True
            )
            return email.rstrip()
        except Exception, e:
            logging.warn("get_email: " + str(e))
            pass

    return get_username() + "@%(server)s"


@memoize
def get_fullname():
    """Get full name of the user.
    """
    if is_git_available():
        try:
            fullname = subprocess.check_output(
                "git config --get user.name 2>/dev/null", shell=True
            )
            return fullname.rstrip()
        except Exception, e:
            logging.warn("get_fullname: " + str(e))
            pass

    return os.environ.get("FULLNAME", False) or get_username()


@memoize
def get_distribution():
    """Get name and version of the distribution of the system based on
    heuristics.
    """
    fedora_f = "/etc/fedora-release"
    rhel_f = "/etc/redhat-release"

    if os.path.exists(fedora_f):
        name = "fedora"
        m = re.match(r"^Fedora .+ ([0-9]+) .+$", open(fedora_f).read())
        version = m is None and "14" or m.groups()[0]

    elif os.path.exists(rhel_f):
        name = "rhel"
        m = re.match(r"^Red Hat.* ([0-9]+) .*$", open(fedora_f).read())
        version = m is None and "6" or m.groups()[0]
    else:
        raise RuntimeError("Not supported distribution!")

    return (name, version)


def rpm_header_from_rpmfile(rpmfile):
    """Read rpm.hdr from rpmfile.
    """
    return rpm.TransactionSet().hdrFromFdno(open(rpmfile, "rb"))


@memoize
def is_noarch(srpm):
    """Determine if given srpm is noarch (arch-independent).
    """
    return rpm_header_from_rpmfile(srpm)["arch"] == "noarch"


def mock_cfg_add_repos(repo, dist, repos_content, templates=TEMPLATES):
    """
    Updated mock.cfg with addingg repository definitions in
    given content and returns it.

    @repo  Repo object
    @dist  Distribution object
    @repos_content  str  Repository definitions to add into mock.cfg
    """
    cfg_opts = Distribution.mockcfg_opts(dist.mockcfg())

    cfg_opts["root"] = repo.buildroot(dist)
    cfg_opts["myrepo_distname"] = dist.name
    cfg_opts["yum.conf"] += "\n\n" + repos_content

    tmpl = templates.get("mock.cfg", "")

    return compile_template(tmpl, {"cfg": cfg_opts})


class Distribution(object):

    def __init__(self, dist, arch="x86_64", bdist_label=None):
        """
        @dist  str   Distribution label, e.g. "fedora-14"
        @arch  str   Architecture, e.g. "i386"
        @bdist_label  str  Distribution label to build, e.g. "fedora-14-i386"
        """
        self.label = "%s-%s" % (dist, arch)
        (self.name, self.version) = self.parse_dist(dist)
        self.arch = arch

        self.arch_pattern = (arch == "i386" and "i*86" or self.arch)

        self.bdist_label = bdist_label is None and self.label or bdist_label

        self.mock_config_opts = self.mockcfg_opts(self.mockcfg())

    @classmethod
    def parse_dist(self, dist):
        return dist.rsplit("-", 1)

    @classmethod
    def mockcfg_opts(self, mockcfg_path):
        """
        Load mock config file and returns $mock_config["config_opts"] as a dict.
        """
        cfg = dict()
        cfg["config_opts"] = dict()

        execfile(mockcfg_path, cfg)

        return cfg["config_opts"]

    def mockcfg_opts_get(self, key, fallback=None):
        return self.mock_config_opts.get(key, fallback)

    def buildroot(self):
        return self.mockcfg_opts_get("root", self.bdist_label)

    def mockdir(self):
        return "/var/lib/mock/%s/result" % self.buildroot()

    def mockcfg(self):
        return "/etc/mock/%s.cfg" % self.bdist_label

    def build_cmd(self, srpm):
        """
        NOTE: mock will print log messages to stderr (not stdout).
        """
        # suppress log messages from mock in accordance with log level:
        if logging.getLogger().level >= logging.WARNING:
            fmt = "mock -r %s %s > /dev/null 2> /dev/null"
        else:
            fmt = "mock -r %s %s"

        return fmt % (self.bdist_label, srpm)


class RpmOperations(object):
    """RPM related operations.
    """

    @classmethod
    def sign_rpms(cls, keyid, rpms):
        """TODO: It might ask user about the gpg passphrase everytime this
        method is called.  How to store the passphrase or streamline with
        gpg-agent via rpm?

        @keyid   GPG Key ID to sign with
        @rpms    RPM file path list

        FIXME: replace os.system() with other way.
        """
        rpms = " ".join(rpms)
        c = "rpm --resign"
        c += " --define \"_signature %s\" --define \"_gpg_name %s\" %s" % \
            ("gpg", keyid, rpms)
        rc = os.system(c)

        return rc

    @classmethod
    def build_cmds(cls, repo, srpm):
        TC = ThreadedCommand
        return [
            TC(repo.build_cmd(srpm, d), timeout=repo.timeout) for d in \
                repo.dists_by_srpm(srpm)
        ]

    @classmethod
    def build(cls, repo, srpm, wait=WAIT_FOREVER):
        cs = cls.build_cmds(repo, srpm)
        rs = prun_and_get_results(cs, wait)

        return rs


class RepoOperations(object):
    """Yum repository operations.
    """
    rpmops = RpmOperations

    @classmethod
    def destdir(cls, repo):
        return os.path.join(repo.topdir, repo.distdir)

    @classmethod
    def sign_rpms(cls, keyid, rpms):
        return cls.rpmops.sign_rpms(keyid, rpms)

    @classmethod
    def build(cls, repo, srpm, wait=WAIT_FOREVER):
        return cls.rpmops.build(repo, srpm, wait)

    @classmethod
    def deploy(cls, repo, srpm, build=True, build_wait=WAIT_FOREVER,
            deploy_wait=WAIT_FOREVER):
        """
        FIXME: ugly code around signkey check.
        """
        if build:
            rs = cls.build(repo, srpm, build_wait)
            assert all(rc == 0 for rc in rs)

        destdir = cls.destdir(repo)
        rpms_to_deploy = []   # :: [(rpm_path, destdir)]

        #rpms_to_deploy.append((srpm, os.path.join(destdir, "sources")))

        for d in repo.dists_by_srpm(srpm):
            srpm_to_copy = glob.glob("%s/*.src.rpm" % d.mockdir())[0]
            rpms_to_deploy.append(
                (srpm_to_copy, os.path.join(destdir, "sources"))
            )

            rpms = [
                f for f in glob.glob("%s/*.*.rpm" % d.mockdir())\
                    if not f.endswith(".src.rpm")
            ]
            logging.debug("rpms=" + str([os.path.basename(f) for f in rpms]))

            for p in rpms:
                rpms_to_deploy.append((p, os.path.join(destdir, d.arch)))

        if repo.signkey:
            # We don't need to sign SRPM:
            rpms_to_sign = [
                rpm for rpm, _dest in rpms_to_deploy \
                    if not rpm.endswith("src.rpm")
            ]

            cls.sign_rpms(repo.signkey, rpms_to_sign)

        cs = [
            ThreadedCommand(repo.copy_cmd(rpm, dest)) for rpm, dest \
                in rpms_to_deploy
        ]
        rs = prun_and_get_results(cs, deploy_wait)
        assert all(rc == 0 for rc in rs)

        cls.update(repo)

    @classmethod
    def deploy_mock_cfg_rpm(cls, repo, workdir, release_file_content):
        """Generate mock.cfg files and corresponding RPMs.
        """
        mockcfgdir = os.path.join(workdir, "etc", "mock")
        os.makedirs(mockcfgdir)

        mock_cfg_files = []

        for dist in repo.dists:
            mc = repo.mock_file_content(dist, release_file_content)
            mock_cfg_path = os.path.join(
                mockcfgdir, "%s-%s.cfg" % (repo.name, dist.label)
            )

            open(mock_cfg_path, "w").write(mc)

            mock_cfg_files.append(mock_cfg_path)

        listfile_path = os.path.join(workdir, "mockcfg.files.list")
        open(listfile_path, "w").write(
            "\n".join(
                "%s,rpmattr=%%config(noreplace)" % mcfg \
                    for mcfg in mock_cfg_files) + "\n"
        )

        rc = run(
            repo.mock_cfg_rpm_build_cmd(workdir, listfile_path),
            repo.user,
            timeout=BUILD_TIMEOUT
        )
        if rc != 0:
            raise RuntimeError("Failed to create mock.cfg rpm")

        srpms = glob.glob(
            "%(wdir)s/mock-data-%(repon)s-%(dver)s/mock-data-*.src.rpm" % \
                {"wdir": workdir, "repo": repo.name, "dver": repo.distversion}
        )
        if not srpms:
            logging.error("Failed to build src.rpm")
            sys.exit(1)

        srpm = srpms[0]

        cls.deploy(repo, srpm)
        cls.update(repo)

    @classmethod
    def deploy_release_rpm(cls, repo, workdir=None):
        """Generate (yum repo) release package.

        @workdir str   Working directory
        """
        if workdir is None:
            workdir = tempfile.mkdtemp(dir="/tmp",
                                       prefix="%s-release-" % repo.name
                                       )

        rfc = repo.release_file_content()

        cls.deploy_mock_cfg_rpm(repo, workdir, rfc)

        reldir = os.path.join(workdir, "etc", "yum.repos.d")
        os.makedirs(reldir)

        release_file_path = os.path.join(reldir, "%s.repo" % repo.name)
        open(release_file_path, 'w').write(rfc)

        if repo.signkey:
            keydir = os.path.join(workdir, repo.keydir[1:])
            os.makedirs(keydir)

            rc = run(
                "gpg --export --armor %s > ./%s" % (repo.signkey, repo.keyfile),
                workdir=workdir,
                timeout=MIN_TIMEOUT,
            )

            release_file_list = os.path.join(workdir, "files.list")
            open(release_file_list, "w").write(
                release_file_path + ",rpmattr=%config\n" + workdir + \
                    repo.keyfile + "\n"
            )

        rc = run(
            repo.release_rpm_build_cmd(workdir, release_file_path),
            repo.user,
            timeout=BUILD_TIMEOUT
        )

        srpms = glob.glob(
            "%s/%s-release-%s/%s-release*.src.rpm" % \
                (workdir, repo.name, repo.distversion, repo.name)
        )
        if not srpms:
            logging.error("Failed to build src.rpm")
            sys.exit(1)

        srpm = srpms[0]

        cls.deploy(repo, srpm)
        cls.update(repo)

    @classmethod
    def init(cls, repo):
        """Initialize yum repository.
        """
        destdir = cls.destdir(repo)

        rc = run(
            "mkdir -p " + " ".join(repo.rpmdirs(destdir)),
            repo.user, repo.server,
            timeout=repo.timeout
        )

        cls.deploy_release_rpm(repo)

    @classmethod
    def update(cls, repo):
        """'createrepo --update ...', etc.
        """
        destdir = cls.destdir(repo)
        _TC = ThreadedCommand

        # hack: degenerate noarch rpms
        if len(repo.archs) > 1:
            c = "for d in %s; "
            c += "   do (cd $d && ln -sf ../%s/*.noarch.rpm ./); "
            c += "done"
            c = c % (" ".join(repo.archs[1:]), repo.dists[0].arch)

            cmd = _TC(c, repo.user, repo.server, destdir, timeout=repo.timeout)
            cmd.run()

        c = "test -d repodata"
        c += " && createrepo --update --deltas --oldpackagedirs . --database ."
        c += " || createrepo --deltas --oldpackagedirs . --database ."

        cs = [
            _TC(c, repo.user, repo.server, d, timeout=repo.timeout) for d \
                in repo.rpmdirs(destdir)
        ]

        rs = prun_and_get_results(cs)
        return rs


class Repo(object):
    """Yum repository.
    """
    name = "%(distname)s-%(hostname)s-%(user)s"
    subdir = "yum"
    topdir = "~%(user)s/public_html/%(subdir)s"
    baseurl = "http://%(server)s/%(user)s/%(subdir)s/%(distdir)s"

    signkey = ""
    keydir = "/etc/pki/rpm-gpg"
    keyurl = "file://%(keydir)s/RPM-GPG-KEY-%(name)s-%(distversion)s"

    metadata_expire = "2h"

    release_file_tmpl = """\
[${repo.name}]
name=Custom yum repository on ${repo.server} by ${repo.user} (\$basearch)
baseurl=${repo.baseurl}/\$basearch/
metadata_expire=${repo.metadata_expire}
enabled=1
#if $repo.signkey
gpgcheck=1
gpgkey=${repo.keyurl}
#else
gpgcheck=0
#end if

[${repo.name}-source]
name=Custom yum repository on ${repo.server} by ${repo.user} (source)
baseurl=${repo.baseurl}/sources/
metadata_expire=${repo.metadata_expire}
enabled=0
gpgcheck=0
"""
    release_file_build_tmpl = """\
#if not $repo.signkey
echo "${repo.release_file},rpmattr=%config" | \\
#end if
pmaker -n ${repo.name}-release --license MIT \\
    -w ${repo.workdir} \\
    --itype filelist \\
    --upto sbuild \\
    --group "System Environment/Base" \\
    --url ${repo.baseurl} \\
    --summary "Yum repo files for ${repo.name}" \\
    --packager "${repo.fullname}" \\
    --email "${repo.email}" \\
    --pversion ${repo.distversion}  \\
    --no-rpmdb --no-mock \\
    --ignore-owner \\
    ${repo.logopt} \\
    --destdir ${repo.workdir} \\
#if $repo.signkey
$repo.release_file_list
#else
-
#end if
"""

    mock_cfg_rpm_build_tmpl = """\
pmaker -n mock-data-${repo.name} \\
    --license MIT \\
    -w ${repo.workdir} \\
    --itype filelist \\
    --upto sbuild \\
    --group "Development/Tools" \\
    --url ${repo.baseurl} \\
    --summary "Mock cfg files of yum repo ${repo.name}" \\
    --packager "${repo.fullname}" \\
    --email "${repo.email}" \\
    --pversion ${repo.distversion}  \\
    --no-rpmdb --no-mock \\
    --ignore-owner \\
    --destdir ${repo.workdir} \\
    $repo.mock_cfg_file_list
"""

    def __init__(self, server, user, email, fullname, dist, archs,
            name=None, subdir=None, topdir=None, baseurl=None, signkey=None,
            bdist_label=None, metadata_expire=None, timeout=None,
            *args, **kwargs):
        """
        @server    server's hostname to provide this yum repo
        @user      username on the server
        @email     email address or its format string
        @fullname  full name, e.g. "John Doe".
        @name      repository name or its format string, e.g. "rpmfusion-free",
                   "%(distname)s-%(hostname)s-%(user)s"
        @dist      distribution string, e.g. "fedora-14"
        @archs     architecture list, e.g. ["i386", "x86_64"]
        @subdir    repo's subdir
        @topdir    repo's topdir or its format string, e.g.
                   "/var/www/html/%(subdir)s".
        @baseurl   base url or its format string, e.g. "file://%(topdir)s".
        @signkey   GPG key ID for signing or None indicates will never sign rpms
        @bdist_label  Distribution label to build srpms, e.g.
                   "fedora-custom-addons-14-x86_64"
        @metadata_expire  Metadata expiration time, e.g. "2h", "1d"
        @timeout   Timeout
        """
        self.server = server
        self.user = user
        self.fullname = fullname
        self.dist = dist
        self.archs = archs

        self.hostname = server.split('.')[0]
        self.multiarch = "i386" in self.archs and "x86_64" in self.archs

        self.bdist_label = bdist_label

        (self.distname, self.distversion) = Distribution.parse_dist(self.dist)
        self.dists = [
            Distribution(self.dist, arch, bdist_label) for arch in self.archs
        ]
        self.distdir = os.path.join(
            self.dists[0].mockcfg_opts_get("myrepo_distname", self.distname),
            self.distversion
        )

        self.subdir = subdir is None and self.subdir or subdir

        self.email = self._format(email)

        if name is None:
            name = Repo.name

        if topdir is None:
            topdir = Repo.topdir

        if baseurl is None:
            baseurl = Repo.baseurl

        # expand parameters in format strings:
        self.name = self._format(name)
        self.topdir = self._format(topdir)
        self.baseurl = self._format(baseurl)

        self.keydir = Repo.keydir

        if signkey is None:
            self.signkey = self.keyurl = self.keyfile = ""
        else:
            self.signkey = signkey
            self.keyurl = self._format(Repo.keyurl)
            self.keyfile = os.path.join(self.keydir, os.path.basename(self.keyurl))

        if metadata_expire is not None:
            self.metadata_expire = metadata_expire

        self.timeout = timeout

    def _format(self, fmt_or_var):
        return "%" in fmt_or_var and fmt_or_var % self.__dict__ or fmt_or_var

    def buildroot(self, dist):
        return "%s-%s" % (self.name, dist.label)

    def rpmdirs(self, destdir=None):
        f = destdir is None and snd or os.path.join

        return [f(destdir, d) for d in ["sources"] + self.archs]

    def copy_cmd(self, src, dst):
        if is_local(self.server):
            cmd = "cp -a %s %s" % \
                (src, ("~" in dst and os.path.expanduser(dst) or dst))
        else:
            cmd = "scp -p %s %s@%s:%s" % (src, self.user, self.server, dst)

        return cmd

    def build_cmd(self, srpm, dist):
        """Returns Command object to build src.rpm
        """
        return dist.build_cmd(srpm)

    def dists_by_srpm(self, srpm):
        return (is_noarch(srpm) and self.dists[:1] or self.dists)

    def release_file_content(self):
        # this package will be noarch (arch-independent).
        dist = self.dists[0]
        params = {"repo": self, "dist": dist}

        return compile_template(self.release_file_tmpl, params)

    def mock_file_content(self, dist, release_file_content=None):
        """
        Returns the content of mock.cfg for given dist.

        @dist  Distribution  Distribution object
        @release_file_content  str  The content of this repo's release file
        """
        if release_file_content is None:
            release_file_content = self.release_file_content()

        return mock_cfg_add_repos(self, dist, release_file_content)

    def release_rpm_build_cmd(self, workdir, release_file_path):
        logopt = logging.getLogger().level < logging.INFO and "--verbose" or ""

        repo = copy.copy(self.__dict__)
        repo.update({
            "release_file": release_file_path,
            "workdir": workdir,
            "logopt": logopt,
            "release_file_list": os.path.join(workdir, "files.list"),
        })
        params = {"repo": repo}

        return compile_template(self.release_file_build_tmpl, params)

    def mock_cfg_rpm_build_cmd(self, workdir, mock_cfg_file_list_path):
        repo = copy.copy(self.__dict__)
        repo.update({
            "workdir": workdir,
            "mock_cfg_file_list": mock_cfg_file_list_path
        })
        params = {"repo": repo}

        return compile_template(self.mock_cfg_rpm_build_tmpl, params)


def parse_conf_value(s):
    """Simple and naive parser to parse value expressions in config files.

    >>> assert 0 == parse_conf_value("0")
    >>> assert 123 == parse_conf_value("123")
    >>> assert True == parse_conf_value("True")
    >>> assert [1,2,3] == parse_conf_value("[1,2,3]")
    >>> assert "a string" == parse_conf_value("a string")
    >>> assert "0.1" == parse_conf_value("0.1")
    """
    intp = re.compile(r"^([0-9]|([1-9][0-9]+))$")
    boolp = re.compile(r"^(true|false)$", re.I)
    listp = re.compile(r"^(\[\s*((\S+),?)*\s*\])$")

    def matched(pat, s):
        m = pat.match(s)
        return m is not None

    if not s:
        return ""

    if matched(boolp, s):
        return bool(s)

    if matched(intp, s):
        return int(s)

    if matched(listp, s):
        return eval(s)  # TODO: too danger. safer parsing should be needed.

    return s


def init_defaults_by_conffile(config=None, profile=None):
    """
    Initialize default values for options by loading config files.
    """
    if config is None:
        # Is there case that $HOME is empty?
        home = os.environ.get("HOME", os.curdir)

        confs = ["/etc/myreporc"]
        confs += sorted(glob.glob("/etc/myrepo.d/*.conf"))
        confs += [os.environ.get("MYREPORC", os.path.join(home, ".myreporc"))]
    else:
        confs = (config,)

    cparser = cp.SafeConfigParser()
    loaded = False

    for c in confs:
        if os.path.exists(c):
            logging.info("Loading config: %s" % c)
            cparser.read(c)
            loaded = True

    if not loaded:
        return {}

    d = profile and cparser.items(profile) or cparser.defaults().iteritems()

    return dict((k, parse_conf_value(v)) for k, v in d)


def init_defaults():
    dists_full = list_dists()
    archs = list_archs()

    dists = ["%s-%s" % get_distribution()]

    distributions_full = ["%s-%s" % da for da in product(dists_full, archs)]
    distributions = ["%s-%s" % da for da in product(dists, archs)]

    defaults = {
        "server": hostname(),
        "user": get_username(),
        "email":  get_email(),
        "fullname": get_fullname(),
        "dists_full": ",".join(distributions_full),
        "dists": ",".join(distributions),
        "name": Repo.name,
        "subdir": Repo.subdir,
        "topdir": Repo.topdir,
        "baseurl": Repo.baseurl,
        "signkey": Repo.signkey,
        "metadata_expire": Repo.metadata_expire,
    }

    if is_local(defaults["server"]):
        timeout = LOCAL_TIMEOUT
    else:
        timeout = REMOTE_TIMEOUT

    defaults["timeout"] = timeout

    return defaults


def parse_dist_option(dist_str, sep=":"):
    """Parse dist_str and returns (dist, arch, bdist_label).

    SEE ALSO: parse_dists_option (below)

    >>> try:
    ...     parse_dist_option("invalid_dist_label.i386")
    ... except AssertionError:
    ...     pass
    >>> parse_dist_option("fedora-14-i386")
    ('fedora-14', 'i386', 'fedora-14-i386')
    >>> parse_dist_option("fedora-14-i386:fedora-my-additions-14-i386")
    ('fedora-14', 'i386', 'fedora-my-additions-14-i386')
    >>> parse_dist_option("fedora-14-i386:fedora-my-additions-14-x86_64")
    ('fedora-14', 'i386', 'fedora-my-additions-14-x86_64')
    >>> parse_dist_option("fedora-14-i386:fedora-my-additions")
    ('fedora-14', 'i386', 'fedora-my-additions')
    """
    tpl = dist_str.split(sep)
    label = tpl[0]

    assert "-" in label, "Invalid distribution label ('-' not found): " + label

    (dist, arch) = label.rsplit("-", 1)

    if len(tpl) < 2:
        bdist_label = label
    else:
        bdist_label = tpl[1]

        if len(tpl) > 2:
            m = "Invalid format: too many '%s' in dist_str: %s. " + \
                "Ignore the rest"
            logging.warn(m % (sep, dist_str))

    return (dist, arch, bdist_label)


def parse_dists_option(dists_str, sep=","):
    """Parse --dists option and returns [(dist, arch, bdist_label)].

    # d[:d.rfind("-")])
    #archs = [l.split("-")[-1] for l in labels]

    >>> parse_dists_option("fedora-14-i386")
    [('fedora-14', 'i386', 'fedora-14-i386')]
    >>> parse_dists_option("fedora-14-i386:fedora-my-additions-14-i386")
    [('fedora-14', 'i386', 'fedora-my-additions-14-i386')]
    >>> ss = ["fedora-14-i386:fedora-my-additions-14-i386"]
    >>> ss += ["rhel-6-i386:rhel-my-additions-6-i386"]
    >>> parse_dists_option(",".join(ss))
    [('fedora-14', 'i386', 'fedora-my-additions-14-i386'), ('rhel-6', 'i386', 'rhel-my-additions-6-i386')]
    """
    return [parse_dist_option(dist_str) for dist_str in dists_str.split(sep)]


def opt_parser():
    defaults = init_defaults()
    distribution_choices = defaults["dists_full"]  # save it.

    defaults.update(init_defaults_by_conffile())

    p = optparse.OptionParser("""%prog COMMAND [OPTION ...] [ARGS]

Commands: i[init], b[uild], d[eploy], u[pdate]

Examples:
  # initialize your yum repos:
  %prog init -s yumserver.local -u foo -m foo@example.com -F "John Doe"

  # build SRPM:
  %prog build packagemaker-0.1-1.src.rpm

  # build SRPM and deploy RPMs and SRPMs into your yum repos:
  %prog deploy packagemaker-0.1-1.src.rpm
  %prog d --dists rhel-6-x86_64 packagemaker-0.1-1.src.rpm
  """
    )

    for k in ("verbose", "quiet", "debug"):
        if not defaults.get(k, False):
            defaults[k] = False

    p.set_defaults(**defaults)

    p.add_option("-C", "--config", help="Configuration file")
    p.add_option("-T", "--timeout", type="int",
        help="Timeout [sec] for each operations [%default]")

    p.add_option("-s", "--server", help="Server to provide your yum repos.")
    p.add_option("-u", "--user", help="Your username on the server [%default]")
    p.add_option("-m", "--email",
        help="Your email address or its format string [%default]")
    p.add_option("-F", "--fullname", help="Your full name [%default]")

    p.add_option("", "--dists",
        help="Comma separated distribution labels including arch "
        "(optionally w/ build (mock) distribution label). "
        "Options are some of " + distribution_choices + " [%default] "
        "and these combinations: e.g. fedora-16-x86_64, "
        "rhel-6-i386:my-custom-addon-rhel-6-i386"
    )

    p.add_option("-q", "--quiet", action="store_true", help="Quiet mode")
    p.add_option("-v", "--verbose", action="store_true", help="Verbose mode")
    p.add_option("", "--debug", action="store_true", help="Debug mode")

    iog = optparse.OptionGroup(p, "Options for 'init' command")
    iog.add_option('', "--name",
        help="Name of your yum repo or its format string [%default].")
    iog.add_option("", "--subdir",
        help="Repository sub dir name [%default]")
    iog.add_option("", "--topdir",
        help="Repository top dir or its format string [%default]")
    iog.add_option('', "--baseurl",
        help="Repository base URL or its format string [%default]")
    iog.add_option('', "--signkey",
        help="GPG key ID if signing RPMs to deploy")
    p.add_option_group(iog)

    return p


def do_command(cmd, repos, srpm=None, wait=WAIT_FOREVER):
    f = getattr(RepoOperations, cmd)
    threads = []

    if srpm is not None:
        is_noarch(srpm)  # make a result cache

    for repo in repos:
        args = srpm is None and (repo, ) or (repo, srpm)

        thread = threading.Thread(target=f, args=args)
        thread.start()

        threads.append(thread)

    time.sleep(MIN_TIMEOUT)

    for thread in threads:
        # it will block.
        thread.join(wait)

        # Is there any possibility thread still live?
        if thread.is_alive():
            logging.info("Terminating the thread")

            thread.join()


def main(argv=sys.argv):
    (CMD_INIT, CMD_UPDATE, CMD_BUILD, CMD_DEPLOY) = \
        ("init", "update", "build", "deploy")

    logformat = "%(asctime)s [%(levelname)-4s] myrepo: %(message)s"
    logdatefmt = "%H:%M:%S"  # too much? "%a, %d %b %Y %H:%M:%S"

    logging.basicConfig(format=logformat, datefmt=logdatefmt)

    p = opt_parser()
    (options, args) = p.parse_args(argv[1:])

    if options.verbose:
        logging.getLogger().setLevel(logging.INFO)
    elif options.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    elif options.quiet:
        logging.getLogger().setLevel(logging.ERROR)
    else:
        logging.getLogger().setLevel(logging.WARN)

    if not args:
        p.print_usage()
        sys.exit(1)

    a0 = args[0]
    if a0.startswith('i'):
        cmd = CMD_INIT

    elif a0.startswith('u'):
        cmd = CMD_UPDATE

    elif a0.startswith('b'):
        cmd = CMD_BUILD
        assert len(args) >= 2, \
            "'%s' command requires an argument to specify srpm[s]" % cmd

    elif a0.startswith('d'):
        cmd = CMD_DEPLOY
        assert len(args) >= 2, \
            "'%s' command requires an argument to specify srpm[s]" % cmd

    else:
        logging.error(" Unknown command '%s'" % a0)
        sys.exit(1)

    if options.config:
        params = init_defaults()
        params.update(init_defaults_by_conffile(options.config))

        p.set_defaults(**params)

        # re-parse to overwrite configurations with given options.
        (options, args) = p.parse_args()

    config = copy.copy(options.__dict__)

    # Kept for DEBUG:
    #pprint.pprint(config)
    #sys.exit()

    dabs = parse_dists_option(config["dists"])  # [(dist, arch, bdist_label)]
    repos = []

    # old way:
    #dists = config["dists"].split(",")
    #for dist, labels in groupby(dists, lambda d: d[:d.rfind("-")]):
    #    archs = [l.split("-")[-1] for l in labels]

    # extended new way:
    for dist, dists in groupby(dabs, lambda d: d[0]):  # d[0]: dist
        dists = list(dists)  # it's a generator and has internal state.

        archs = [d[1] for d in dists]  # d[1]: arch
        bdist_label = [d[2] for d in dists][0]  # d[2]: bdist_label

        repo = Repo(
            config["server"],
            config["user"],
            config["email"],
            config["fullname"],
            dist,
            archs,
            config["name"],
            config["subdir"],
            config["topdir"],
            config["baseurl"],
            config["signkey"],
            bdist_label,
            config["metadata_expire"],
            config["timeout"],
        )

        repos.append(repo)

    srpms = args[1:]

    if srpms:
        for srpm in srpms:
            do_command(cmd, repos, srpm)
    else:
        do_command(cmd, repos)

    sys.exit()


if __name__ == '__main__':
    main(sys.argv)


# vim:sw=4 ts=4 et:
