#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
#
# Copyright (C) 2019 Netronome Systems, Inc.
# Copyright (c) 2020 Facebook

import configparser
import datetime
import json
import os
import time
from typing import Dict

from core import NIPA_DIR
from core import log, log_open_sec, log_end_sec, log_init
from core import Tester, TesterAlreadyTested
from core import Tree
from pw import Patchwork
from pw import PwSeries
import netdev


class IncompleteSeries(Exception):
    pass


class PwPoller:
    def __init__(self) -> None:
        config = configparser.ConfigParser()
        config.read(['nipa.config', 'pw.config', 'poller.config'])

        log_init(config.get('log', 'type', fallback='org'),
                 config.get('log', 'file', fallback=os.path.join(NIPA_DIR,
                                                                 "poller.org")))

        # TODO: make this non-static / read from a config
        self._trees = {
            "net-next": Tree("net-next", "net-next", "../net-next", "net-next"),
            "net": Tree("net", "net", "../net", "net"),
        }

        self._tester = Tester(config.get('results', 'dir',
                                         fallback=os.path.join(NIPA_DIR, "results")))

        self._pw = Patchwork(config)

        self._state = {
            'last_poll': (datetime.datetime.utcnow() - datetime.timedelta(hours=2)).timestamp(),
            'seen_series': [],
        }
        self.init_state_from_disk()
        self.seen_series = set(self._state['seen_series'])

    def init_state_from_disk(self) -> None:
        try:
            with open('poller.state', 'r') as f:
                loaded = json.load(f)

                for k in loaded.keys():
                    self._state[k] = loaded[k]
        except FileNotFoundError:
            pass

    def write_tree_selection_result(self, s, comment):
        series_dir = os.path.join(self._tester.result_dir, str(s.id))

        tree_test_dir = os.path.join(series_dir, "tree_selection")
        if not os.path.exists(tree_test_dir):
            os.makedirs(tree_test_dir)

        with open(os.path.join(tree_test_dir, "retcode"), "w+") as fp:
            fp.write("0")
        with open(os.path.join(tree_test_dir, "desc"), "w+") as fp:
            fp.write(comment)

        done_file = os.path.join(series_dir, ".tester_done")
        if os.path.exists(done_file):
            # Real tester has already run and created the real hierarchy
            return

        for patch in s.patches:
            patch_dir = os.path.join(series_dir, str(patch.id))
            if not os.path.exists(patch_dir):
                os.makedirs(patch_dir)

        os.mknod(done_file)

    def series_determine_tree(self, s: PwSeries) -> str:
        log_open_sec('Determining the tree')
        s.tree_name = netdev.series_tree_name_direct(s)
        s.tree_mark_expected = True
        s.tree_marked = bool(s.tree_name)

        if s.tree_name:
            log(f'Series is clearly designated for: {s.tree_name}', "")
            log_end_sec()
            return f"Clearly marked for {s.tree_name}"

        s.tree_mark_expected = netdev.series_tree_name_should_be_local(s)
        if s.tree_mark_expected == False:
            log("No tree designation found or guessed", "")
            log_end_sec()
            return "Not a local patch"

        if netdev.series_ignore_missing_tree_name(s):
            s.tree_mark_expected = None
            log('Okay to ignore lack of tree in subject, ignoring series', "")
            log_end_sec()
            return "Series ignored based on subject"

        log_open_sec('Series should have had a tree designation')
        if netdev.series_is_a_fix_for(s, self._trees["net"]):
            s.tree_name = "net"
        elif self._trees["net-next"].check_applies(s):
            s.tree_name = "net-next"

        if s.tree_name:
            log(f"Target tree - {s.tree_name}", "")
            res = f"Guessed tree name to be {s.tree_name}"
        else:
            log("Target tree not found", "")
            res = "Guessing tree name failed - patch did not apply"
        log_end_sec()

        log_end_sec()
        return res

    def process_series(self, pw_series) -> None:
        log_open_sec(f"Checking series {pw_series['id']} " +
                     f"with {pw_series['total']} patches")

        if pw_series['id'] in self.seen_series:
            log(f"Already seen {pw_series['id']}", "")
            log_end_sec()
            return

        s = PwSeries(self._pw, pw_series)

        log("Series info",
            f"Series ID {s['id']}\n" +
            f"Series title {s['name']}\n" +
            f"Author {s['submitter']['name']}\n" +
            f"Date {s['date']}")
        log_open_sec('Patches')
        for p in s['patches']:
            log(p['name'], "")
        log_end_sec()

        if not s['received_all']:
            raise IncompleteSeries

        comment = self.series_determine_tree(s)

        try:
            if hasattr(s, 'tree_name') and s.tree_name:
                series_ret, patch_ret = self._tester.test_series(self._trees[s.tree_name], s)

            self.write_tree_selection_result(s, comment)
        except TesterAlreadyTested:
            log("Warning: series was already tested!")

        log_end_sec()

        self.seen_series.add(s['id'])

    def run(self) -> None:
        partial_series = {}

        prev_big_scan = datetime.datetime.fromtimestamp(self._state['last_poll'])
        prev_req_time = datetime.datetime.utcnow()

        # We poll every 2 minutes, for series from last 4 minutes
        # Every 3 hours we do a larger check of series of last 12 hours to make sure we didn't miss anything
        # apparently patchwork uses the time from the email headers and people back date their emails, a lot
        # We keep a history of the series we've seen in and since the last big poll to not process twice
        try:
            while True:
                this_poll_seen = set()
                req_time = datetime.datetime.utcnow()

                # Decide if this is a normal 4 minute history poll or big scan of last 12 hours
                if prev_big_scan + datetime.timedelta(hours=3) < req_time:
                    big_scan = True
                    since = prev_big_scan - datetime.timedelta(hours=9)
                    log_open_sec(f"Big scan of last 12 hours at {self._state['last_poll']} since {since}")
                else:
                    big_scan = False
                    since = prev_req_time - datetime.timedelta(minutes=4)
                    log_open_sec(f"Checking at {self._state['last_poll']} since {since}")

                json_resp = self._pw.get_series_all(since=since)
                log(f"Loaded {len(json_resp)} series", "")

                had_partial_series = False
                for pw_series in json_resp:
                    try:
                        self.process_series(pw_series)
                        this_poll_seen.add(pw_series['id'])
                    except IncompleteSeries:
                        partial_series.setdefault(pw_series['id'], 0)
                        if partial_series[pw_series['id']] < 5:
                            had_partial_series = True
                        partial_series[pw_series['id']] += 1

                if big_scan:
                    prev_req_time = req_time
                    prev_big_scan = req_time
                    # Shorten the history of series we've seen to just the last 12 hours
                    self.seen_series = this_poll_seen
                elif had_partial_series:
                    log("Partial series, not moving time forward", "")
                else:
                    prev_req_time = req_time

                time.sleep(120)
                log_end_sec()
        finally:
            self._state['last_poll'] = prev_big_scan.timestamp()
            self._state['seen_series'] = list(self.seen_series)
            # Dump state
            with open('poller.state', 'w') as f:
                json.dump(self._state, f)


if __name__ == "__main__":
    poller = PwPoller()
    poller.run()
