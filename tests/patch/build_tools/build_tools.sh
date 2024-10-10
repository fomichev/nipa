#!/bin/bash
# SPDX-License-Identifier: GPL-2.0

ncpu=$(grep -c processor /proc/cpuinfo)
build_flags="-Oline -j $ncpu"
tmpfile_o=$(mktemp)
tmpfile_n=$(mktemp)
tmpfile_do=$(mktemp)
tmpfile_dn=$(mktemp)
rc=0

pr() {
    echo " ====== $@ ======" | tee -a /dev/stderr
}

mrproper() {
    pr "Forcefully cleaning"
    git clean -xf tools/testing/selftests/
    git clean -xf tools/net/ynl/
}

clean_up() {
    pr "Cleaning"
    make $build_flags -C tools/testing/selftests/ clean

    # Hard-clean YNL, too, otherwise YNL-related build problems may be masked
    make -C tools/net/ynl/ distclean
}

dirty_files() {
	git status -s
}

# If it doesn't touch tools/ or include/, don't bother
if ! git diff --name-only HEAD~ | grep -q -E "^(include)|(tools)/"; then
    echo "No tools touched, skip" >&$DESC_FD
    exit 0
fi

# Looks like tools inherit WERROR, otherwise
trap "make mrproper" EXIT
make allmodconfig
./scripts/config -d werror

echo "Using $build_flags redirect to $tmpfile_o and $tmpfile_n"

HEAD=$(git rev-parse HEAD)

echo "Tree base:"
git log -1 --pretty='%h ("%s")' HEAD~
echo "Now at:"
git log -1 --pretty='%h ("%s")' HEAD

# These are either very slow or don't build
export SKIP_TARGETS="bpf dt landlock livepatch lsm user_events mm powerpc"

mrproper

pr "Baseline building the tree"
git checkout -q HEAD~
make $build_flags headers
make $build_flags -C tools/testing/selftests/
git checkout -q $HEAD

mrproper

pr "Building the tree before the patch"
git checkout -q HEAD~

make $build_flags headers
make $build_flags -C tools/testing/selftests/ \
     2> >(tee -a $tmpfile_o >&2)

incumbent=$(grep -i -c "\(warn\|error\)" $tmpfile_o)

pr "Checking if tree is clean"
dirty_files > $tmpfile_do
clean_up
git clean -ndxf >> $tmpfile_do
incumbent_dirt=$(cat $tmpfile_do | wc -l)

mrproper

pr "Building the tree with the patch"
git checkout -q $HEAD

make $build_flags headers
make $build_flags -C tools/testing/selftests/ \
     2> >(tee -a $tmpfile_n >&2)

current=$(grep -i -c "\(warn\|error\)" $tmpfile_n)

pr "Checking if tree is clean"
dirty_files > $tmpfile_dn
clean_up
git clean -ndxf >> $tmpfile_dn
current_dirt=$(cat $tmpfile_dn | wc -l)

echo "Errors and warnings before: $incumbent (+$incumbent_dirt) this patch: $current (+$current_dirt)" >&$DESC_FD

if [ $current -gt $incumbent ]; then
  echo "New errors added" 1>&2
  diff -U 0 $tmpfile_o $tmpfile_n 1>&2

  echo "Per-file breakdown" 1>&2
  tmpfile_fo=$(mktemp)
  tmpfile_fn=$(mktemp)

  grep -i "\(warn\|error\)" $tmpfile_o | sed -n 's@\(^\.\./[/a-zA-Z0-9_.-]*.[ch]\):.*@\1@p' | sort | uniq -c \
    > $tmpfile_fo
  grep -i "\(warn\|error\)" $tmpfile_n | sed -n 's@\(^\.\./[/a-zA-Z0-9_.-]*.[ch]\):.*@\1@p' | sort | uniq -c \
    > $tmpfile_fn

  diff -U 0 $tmpfile_fo $tmpfile_fn 1>&2
  rm $tmpfile_fo $tmpfile_fn

  rc=1
fi

if [ $current_dirt -gt $incumbent_dirt ]; then
    echo "New untracked files added" 1>&2
    diff -U 0 $tmpfile_do $tmpfile_dn 1>&2

    rc=1
fi

rm $tmpfile_o $tmpfile_n $tmpfile_do $tmpfile_dn

exit $rc
