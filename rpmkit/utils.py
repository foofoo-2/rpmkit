#
# misc utility routines
#
# Copyright (C) 2011, 2012 Red Hat, Inc.
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
from rpmkit.memoize import memoize
from itertools import izip, takewhile

import datetime
import itertools
import operator
import os.path

try:
    from functools import reduce as foldl
except ImportError:
    foldl = reduce

try:
    chain_from_iterable = itertools.chain.from_iterable
except AttributeError:
    # Borrowed from library doc, 9.7.1 Itertools functions:
    def _from_iterable(iterables):
        for it in iterables:
            for element in it:
                yield element

    chain_from_iterable = _from_iterable


def typecheck(obj, expected_type_or_class):
    """Type checker.

    :param obj: Target object to check type
    :param expected_type_or_class: Expected type or class of $obj
    """
    if not isinstance(obj, expected_type_or_class):
        m = "Expected %s but got %s type. obj=%s" % (
            repr(expected_type_or_class), type(obj), str(obj),
        )
        raise TypeError(m)


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


def is_foldable(xs):
    """@see http://www.haskell.org/haskellwiki/Foldable_and_Traversable

    >>> is_foldable([])
    True
    >>> is_foldable(())
    True
    >>> is_foldable(x for x in range(3))
    True
    >>> is_foldable(None)
    False
    >>> is_foldable(True)
    False
    >>> is_foldable(1)
    False
    """
    return isinstance(xs, (list, tuple)) or callable(getattr(xs, "next", None))


def _concat(xss):
    """
    >>> _concat([[]])
    []
    >>> _concat((()))
    []
    >>> _concat([[1,2,3],[4,5]])
    [1, 2, 3, 4, 5]
    >>> _concat([[1,2,3],[4,5,[6,7]]])
    [1, 2, 3, 4, 5, [6, 7]]
    >>> _concat(((1,2,3),(4,5,[6,7])))
    [1, 2, 3, 4, 5, [6, 7]]
    >>> _concat(((1,2,3),(4,5,[6,7])))
    [1, 2, 3, 4, 5, [6, 7]]
    >>> _concat((i, i*2) for i in range(3))
    [0, 0, 1, 2, 2, 4]
    """
    return list(chain_from_iterable(xs for xs in xss))


def _flatten(xss):
    """
    >>> _flatten([])
    []
    >>> _flatten([[1,2,3],[4,5]])
    [1, 2, 3, 4, 5]
    >>> _flatten([[1,2,[3]],[4,[5,6]]])
    [1, 2, 3, 4, 5, 6]

    tuple:

    >>> _flatten([(1,2,3),(4,5)])
    [1, 2, 3, 4, 5]

    generator expression:

    >>> _flatten((i, i * 2) for i in range(0,5))
    [0, 0, 1, 2, 2, 4, 3, 6, 4, 8]
    """
    if is_foldable(xss):
        return foldl(operator.add, (_flatten(xs) for xs in xss), [])
    else:
        return [xss]


def _unique(xs, cmp=cmp, key=None):
    """Returns new sorted list of no duplicated items.

    >>> _unique([])
    []
    >>> _unique([0, 3, 1, 2, 1, 0, 4, 5])
    [0, 1, 2, 3, 4, 5]
    """
    if xs == []:
        return xs

    ys = sorted(xs, cmp=cmp, key=key)

    if ys == []:
        return ys

    ret = [ys[0]]

    for y in ys[1:]:
        if y == ret[-1]:
            continue
        ret.append(y)

    return ret


def uniq(iterable, cmp=cmp, key=None):
    """
    Safer version of the above.
    """
    acc = []
    for x in iterable:
        if x not in acc:
            acc.append(x)

    return acc


def uconcat(xss):
    return uniq(concat(xss))


def groupby_key(xs, keyfunc):
    for k, g in itertools.groupby(sorted(xs, key=keyfunc), key=keyfunc):
        yield (k, list(g))


# FIXME: Looks like bad effects if memoized. Not memoized for a while
concat = _concat
flatten = _flatten
unique = _unique
uniq = _unique


def timeit(f, *args, **kwargs):
    start = datetime.datetime.now()
    ret = f(*args, **kwargs)
    end = datetime.datetime.now()
    return (ret, end - start)


def all_eq(xs):
    """
    True if all items in iterable ``xs`` (list or generator) equals each other.

    >>> all_eq([])
    False
    >>> all_eq(["a", "a", "a"])
    True
    >>> all_eq(c for c in "")
    False
    >>> all_eq(c for c in "aaba")
    False
    >>> all_eq(c for c in "aaaa")
    True
    >>> all_eq([c for c in "aaaa"])
    True
    """
    if not isinstance(xs, list):
        xs = list(xs)  # xs may be a generator...

    return all(x == xs[0] for x in xs[1:]) if xs else False


def is_subseq(sseq, tseq):
    """
    Sequence ``sseq`` is a sub sequence of ``tseq``.

    >>> is_subseq([], [1, 2])
    False
    >>> is_subseq([1, 2], [1, 2, 3])
    True
    >>> is_subseq([1, 2], [0, 1, 2, 3])
    True
    >>> is_subseq([1, 2], [1, 3, 5])
    False
    >>> is_subseq("bcd", "abcde")
    True
    >>> is_subseq((c for c in "bcd"), (c for c in "abcde"))
    True
    """
    # Special optimization for str instances.
    if isinstance(sseq, str):
        if not isinstance(tseq, str):
            tseq = ''.join(tseq)

        return sseq in tseq

    # Convert generators and iterators to lists.
    if not isinstance(sseq, list):
        sseq = list(sseq)

    if not isinstance(tseq, list):
        tseq = list(tseq)

    if not sseq or len(sseq) > len(tseq):
        return False

    head = sseq[0]

    if head not in tseq:
        return False

    while True:
        try:
            idx = tseq.index(head)
            tseq = tseq[idx + 1:]

        except ValueError:  # ``head`` isn't found in the rest of ``tseq``.
            return False

        if all(x == y for x, y in izip(sseq[1:], tseq)):
            return True

    return False


def longest_common_prefix(*xss):
    """
    Variant of LCS = Longest Common Sub-strings.

    For LCS, see http://en.wikipedia.org/wiki/Longest_common_substring_problem

    :param: List of list of any objects which is an instance of Eq type class.
    :return: Common prefix list of given lists.

    >>> longest_common_prefix("abc", "ab", "abcd")
    ['a', 'b']
    >>> longest_common_prefix("abc", "bc")
    []
    >>> longest_common_prefix([1, 2, 3], [1, 2])
    [1, 2]
    >>> longest_common_prefix([1, 2, 3], [])
    []
    """
    return [x[0] for x in takewhile(all_eq, izip(*xss))]


def longest_common_substring(*xss):
    """
    Longest common sub-strings generalized for any iterables.

    >>> longest_common_substring("abcde", "acdebf", "cdeag")
    'cde'
    >>> longest_common_substring([c for c in "abcde"], [c for c in "acdebf"])
    ['c', 'd', 'e']
    >>> longest_common_substring((c for c in "abcde"), (c for c in "acdebf"))
    ['c', 'd', 'e']
    >>> longest_common_substring("", "acdebf", "cdeag")
    []
    """
    # Ensure any item in xss is a list or a str; convert each items to a list
    # if needed.
    xss = [(xs if isinstance(xs, (list, str)) else list(xs)) for xs in xss]

    assert len(xss) > 1, "only an arg found. mulitple strings must be passed."

    def is_substring(ss, xss):
        return ss and all(is_subseq(ss, xs) for xs in xss)

    ss = []

    if not xss[0] or not xss[1]:
        return ss

    for i in range(len(xss[0])):
        for j in range(len(xss[0]) - i + 1):
            candidate = xss[0][i:i + j]

            if j > len(ss) and is_substring(candidate, xss):
                ss = candidate

    return ss


def longest_common_subsequence(s, t):
    """
    Longest common sub string of ``s`` and ``t``.

    >>> longest_common_subsequence("abcde", "acdebf")
    ['a', 'c', 'd', 'e']
    >>> longest_common_subsequence("12340", "01224533324")
    ['1', '2', '3', '4']
    >>> longest_common_subsequence([c for c in "abcde"], [c for c in "acdebf"])
    ['a', 'c', 'd', 'e']
    """
    n = len(s)
    m = len(t)
    dp = [[0 for j in range(m + 1)] for i in range(n + 1)]

    for i, si in enumerate(s):
        for j, tj in enumerate(t):
            if si == tj:
                dp[i + 1][j + 1] = dp[i][j] + 1
            else:
                dp[i + 1][j + 1] = max(dp[i][j + 1], dp[i + 1][j])

    result = []
    (x, y) = (n, m)

    while x != 0 and y != 0:
        if dp[x][y] == dp[x - 1][y]:
            x -= 1
        elif dp[x][y] == dp[x][y - 1]:
            y -= 1
        else:
            assert s[x - 1] == t[y - 1]
            result = [s[x - 1]] + result
            x -= 1
            y -= 1

    return result

# vim:sw=4:ts=4:et:
