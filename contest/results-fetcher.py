#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0

import configparser
import datetime
import json
import os
import requests
import time


"""
Config:

[cfg]
refresh=#secs
[input]
remote_db=/path/to/db
[output]
dir=/path/to/output
url_pfx=relative/within/server
combined=name-of-manifest.json
"""


class FetcherState:
    def __init__(self):
        self.config = configparser.ConfigParser()
        self.config.read(['fetcher.config'])

        # "fetched" is more of a "need state rebuild"
        self.fetched = True


def write_json_atomic(path, data):
    tmp = path + '.new'
    with open(tmp, 'w') as fp:
        json.dump(data, fp)
    os.rename(tmp, path)


def fetch_remote_run(run_info, remote_state):
    r = requests.get(run_info['url'])
    data = json.loads(r.content.decode('utf-8'))

    file = os.path.join(remote_state['dir'], os.path.basename(run_info['url']))
    with open(file, "w") as fp:
        json.dump(data, fp)


def fetch_remote(fetcher, remote, seen):
    print("Fetching remote", remote['url'])
    r = requests.get(remote['url'])
    try:
        manifest = json.loads(r.content.decode('utf-8'))
    except json.decoder.JSONDecodeError:
        print('Failed to decode manifest from remote:', remote['name'])
        return
    remote_state = seen[remote['name']]

    for run in manifest:
        if run['branch'] in remote_state['seen']:
            continue
        if not run['url']:    # Executor has not finished, yet
            fetcher.fetched |= run['branch'] not in remote_state['wip']
            continue

        print('Fetching run', run['branch'])
        fetch_remote_run(run, remote_state)
        fetcher.fetched = True

    with open(os.path.join(remote_state['dir'], 'results.json'), "w") as fp:
        json.dump(manifest, fp)



def build_combined(fetcher, remote_db):
    r = requests.get(fetcher.config.get('input', 'branch_url'))
    branches = json.loads(r.content.decode('utf-8'))
    branch_info = {}
    for br in branches:
        branch_info[br['branch']] = br

    combined = []
    for remote in remote_db:
        name = remote['name']
        dir = os.path.join(fetcher.config.get('output', 'dir'), name)
        print('Combining from remote', name)

        manifest = os.path.join(dir, 'results.json')
        if not os.path.exists(manifest):
            continue

        with open(manifest, "r") as fp:
            results = json.load(fp)

        for entry in results:
            if not entry['url']:    # Executor is running
                if entry['branch'] not in branch_info:
                    continue
                data = entry.copy()
                when = datetime.datetime.fromisoformat(branch_info[entry['branch']]['date'])
                data["start"] = str(when)
                when += datetime.timedelta(hours=2, minutes=58)
                data["end"] = str(when)
                data["results"] = None
            else:
                file = os.path.join(dir, os.path.basename(entry['url']))
                if not os.path.exists(file):
                    print('No file', file)
                    continue
                with open(file, "r") as fp:
                    data = json.load(fp)

            data['remote'] = name
            combined.append(data)
    return combined


def build_seen(fetcher, remote_db):
    seen = {}
    for remote in remote_db:
        seen[remote['name']] = {'seen': set(), 'wip': set()}

        # Prepare local state
        name = remote['name']
        dir = os.path.join(fetcher.config.get('output', 'dir'), name)
        seen[name]['dir'] = dir
        os.makedirs(dir, exist_ok=True)

        url = fetcher.config.get('output', 'url_pfx') + '/' + name
        seen[name]['url'] = url

        # Read the files
        manifest = os.path.join(dir, 'results.json')
        if not os.path.exists(manifest):
            continue

        with open(manifest, "r") as fp:
            results = json.load(fp)
        for entry in results:
            if not entry.get('url'):
                seen[name]['wip'].add(entry.get('branch'))
                print('No URL on', entry, 'from', remote['name'])
                continue
            file = os.path.join(dir, os.path.basename(entry['url']))
            if not os.path.exists(file):
                continue
            seen[name]['seen'].add(entry.get('branch'))
    return seen


def main() -> None:
    fetcher = FetcherState()

    with open(fetcher.config.get('input', 'remote_db'), "r") as fp:
        remote_db = json.load(fp)

    while True:
        if fetcher.fetched:
            seen = build_seen(fetcher, remote_db)
            fetcher.fetched = False

        for remote in remote_db:
            fetch_remote(fetcher, remote, seen)

        if fetcher.fetched:
            print('Generating combined')
            results = build_combined(fetcher, remote_db)

            combined = os.path.join(fetcher.config.get('output', 'dir'),
                                    fetcher.config.get('output', 'combined'))
            write_json_atomic(combined, results)

        time.sleep(int(fetcher.config.get('cfg', 'refresh')))


if __name__ == "__main__":
    main()
