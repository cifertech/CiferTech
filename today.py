"""
GitHub profile README stats generator.
Adapted from Andrew Grant (Andrew6rant), 2022-2025
https://github.com/Andrew6rant/Andrew6rant
"""

import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

# Prefer a PAT (secrets.ACCESS_TOKEN) when set; otherwise GITHUB_TOKEN in Actions.
_TOKEN = os.environ.get('ACCESS_TOKEN') or os.environ.get('GITHUB_TOKEN') or ''
HEADERS = {
    'Accept': 'application/vnd.github+json',
    'User-Agent': 'cifertech-readme-stats',
}
if _TOKEN:
    HEADERS['authorization'] = 'token ' + _TOKEN
USER_NAME = os.environ.get('USER_NAME', 'cifertech')
OWNER_ID = {'id': None}
QUERY_COUNT = {
    'user_getter': 0,
    'follower_getter': 0,
    'graph_repos_stars': 0,
    'recursive_loc': 0,
    'graph_commits': 0,
    'loc_query': 0,
}


def daily_readme(start_date):
    """
    Returns the length of time since the GitHub account was created
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), start_date)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """Returns a properly formatted plural suffix."""
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """Returns a GraphQL request, or raises if the response does not succeed."""
    request = requests.post(
        'https://api.github.com/graphql',
        json={'query': query, 'variables': variables},
        headers=HEADERS,
    )
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """Uses GitHub's GraphQL v4 API to return repository or star counts."""
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if count_type == 'repos':
        return request.json()['data']['user']['repositories']['totalCount']
    if count_type == 'stars':
        return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """Fetch 100 commits from a repository at a time via cursor pagination."""
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                        author {
                                            email
                                            name
                                            user {
                                                id
                                                login
                                            }
                                        }
                                        deletions
                                        additions
                                    }
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post(
        'https://api.github.com/graphql',
        json={'query': query, 'variables': variables},
        headers=HEADERS,
    )
    if request.status_code == 200:
        if request.json()['data']['repository']['defaultBranchRef'] is not None:
            return loc_counter_one_repo(
                owner, repo_name, data, cache_comment,
                request.json()['data']['repository']['defaultBranchRef']['target']['history'],
                addition_total, deletion_total, my_commits,
            )
        return 0, 0, 0
    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception("Too many requests in a short amount of time!\nYou've hit the non-documented anti-abuse limit!")
    raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)


def authored_by_me(author):
    """True if this commit belongs to USER_NAME (id, login, or email/name)."""
    if not author:
        return False
    owner_id = OWNER_ID.get('id') if isinstance(OWNER_ID, dict) else OWNER_ID
    user = author.get('user')
    if user:
        if user.get('id') == owner_id:
            return True
        if (user.get('login') or '').lower() == USER_NAME.lower():
            return True
    email = (author.get('email') or '').lower()
    name = (author.get('name') or '').lower()
    login = USER_NAME.lower()
    if login in email or f'{login}@' in email:
        return True
    if 'cifer' in name:
        return True
    return False


def loc_via_stats(owner, repo_name):
    """LOC from GitHub's contributor stats (additions/deletions by login)."""
    url = f'https://api.github.com/repos/{owner}/{repo_name}/stats/contributors'
    headers = {**HEADERS, 'Accept': 'application/vnd.github+json'}
    payload = None
    for _ in range(6):
        response = requests.get(url, headers=headers, timeout=40)
        if response.status_code == 202:
            time.sleep(3)
            continue
        if response.status_code != 200:
            return None
        payload = response.json()
        break
    if not isinstance(payload, list):
        return None
    addition_total = deletion_total = my_commits = 0
    for person in payload:
        login = ((person.get('author') or {}) or {}).get('login') or ''
        if login.lower() != USER_NAME.lower():
            continue
        for week in person.get('weeks') or []:
            addition_total += week.get('a') or 0
            deletion_total += week.get('d') or 0
            my_commits += week.get('c') or 0
        return addition_total, deletion_total, my_commits
    return 0, 0, 0


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """Recursively count LOC for commits authored by this user."""
    for node in history['edges']:
        if authored_by_me(node['node'].get('author')):
            my_commits += 1
            addition_total += node['node']['additions'] or 0
            deletion_total += node['node']['deletions'] or 0

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    return recursive_loc(
        owner, repo_name, data, cache_comment,
        addition_total, deletion_total, my_commits,
        history['pageInfo']['endCursor'],
    )


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    """Query all repositories and return total lines of code."""
    if edges is None:
        edges = []
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:
        edges += request.json()['data']['user']['repositories']['edges']
        return loc_query(
            owner_affiliation, comment_size, force_cache,
            request.json()['data']['user']['repositories']['pageInfo']['endCursor'],
            edges,
        )
    return cache_builder(
        edges + request.json()['data']['user']['repositories']['edges'],
        comment_size,
        force_cache,
    )


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """Update LOC cache for repositories whose commit count has changed."""
    cached = True
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = loc_via_stats(owner, repo_name)
                    if loc is None:
                        loc = recursive_loc(owner, repo_name, data, cache_comment)
                    if not isinstance(loc, (list, tuple)) or len(loc) < 3:
                        loc = (0, 0, 0)
                    data[index] = (
                        repo_hash + ' '
                        + str(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' '
                        + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
                    )
            except TypeError:
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """Wipe the cache file when the repository list changes."""
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size]
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def force_close_file(data, cache_comment):
    """Save partial cache data if a request fails mid-run."""
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('There was an error while writing to the cache file. The file,', filename, 'has had the partial data saved and closed.')


def stars_counter(data):
    """Count total stars in repositories owned by this user."""
    total_stars = 0
    for node in data:
        total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def rest_get(url, params=None):
    """GET a GitHub REST endpoint and return parsed JSON."""
    response = requests.get(url, headers=HEADERS, params=params, timeout=40)
    response.raise_for_status()
    return response.json()


def rest_user(username):
    """Public profile: id, created_at, followers, public_repos."""
    query_count('user_getter')
    return rest_get(f'https://api.github.com/users/{username}')


def rest_owned_repos(username):
    """All public repos owned by username (paginated)."""
    query_count('graph_repos_stars')
    repos = []
    page = 1
    while True:
        batch = rest_get(
            f'https://api.github.com/users/{username}/repos',
            params={'per_page': 100, 'type': 'owner', 'page': page},
        )
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


def collect_contributor_loc(repos):
    """Sum additions, deletions, and commits for USER_NAME across repos."""
    names = [repo['full_name'] for repo in repos]
    for full in names:
        try:
            requests.get(
                f'https://api.github.com/repos/{full}/stats/contributors',
                headers=HEADERS,
                timeout=30,
            )
        except requests.RequestException:
            pass
    time.sleep(8)
    addition_total = deletion_total = commit_total = 0
    for full in names:
        owner, repo_name = full.split('/')
        loc = loc_via_stats(owner, repo_name)
        if loc is None:
            print(f'  {full}: stats not ready')
            loc = (0, 0, 0)
        else:
            print(f'  {full}: +{loc[0]:,} -{loc[1]:,} commits={loc[2]:,}')
        addition_total += loc[0]
        deletion_total += loc[1]
        commit_total += loc[2]
    return addition_total, deletion_total, addition_total - deletion_total, commit_total


def svg_overwrite(filename, commit_data, star_data, repo_data, contrib_data, follower_data, skip_commits=False):
    """Parse SVG files and update live GitHub stats."""
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'star_data', star_data, 13)
    justify_format(root, 'repo_data', repo_data, 8)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 9)
    if not skip_commits:
        justify_format(root, 'commit_data', commit_data, 24)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """Update element text and the leading dots so values stay aligned."""
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """Find an SVG element by id and replace its text."""
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    """Count total commits from the LOC cache file."""
    total_commits = 0
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'r') as f:
        data = f.readlines()
    data = data[comment_size:]
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    """Return the account ID and creation time of the user."""
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    request = simple_request(user_getter.__name__, query, {'login': username})
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']


def follower_getter(username):
    """Return the number of followers of the user."""
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    """Count how many times the GitHub GraphQL API is called."""
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """Time a function call and return (result, elapsed_seconds)."""
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """Print a formatted time differential."""
    print('{:<23}'.format(' ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    print('Calculation times:')

    profile, user_time = perf_counter(rest_user, USER_NAME)
    OWNER_ID = {'id': profile.get('node_id')}
    formatter('account data', user_time)

    repos, repo_time = perf_counter(rest_owned_repos, USER_NAME)
    repo_data = len(repos)
    star_data = sum(repo.get('stargazers_count') or 0 for repo in repos)
    follower_data = int(profile.get('followers') or 0)
    formatter('repos/stars', repo_time)

    contrib_data = repo_data
    contrib_time = 0
    try:
        contrib_data, contrib_time = perf_counter(
            graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER']
        )
        formatter('contributed', contrib_time)
    except Exception as err:
        print(' contributed (graphql skipped):', err)

    print('Contributor stats:')
    loc_pack, loc_time = perf_counter(collect_contributor_loc, repos)
    loc_add, loc_del, loc_net, commit_data = loc_pack
    formatter('LOC/commits', loc_time)

    skip_commits = commit_data == 0 and loc_add == 0
    if skip_commits:
        print('Stats not ready yet; keeping previous commits in the SVG.')

    svg_overwrite(
        'dark_mode.svg',
        commit_data, star_data, repo_data, contrib_data, follower_data,
        skip_commits=skip_commits,
    )
    svg_overwrite(
        'light_mode.svg',
        commit_data, star_data, repo_data, contrib_data, follower_data,
        skip_commits=skip_commits,
    )

    print('Updated', repo_data, 'repos,', f'{star_data:,}', 'stars,', f'{follower_data:,}', 'followers')
    if not skip_commits:
        print('Commits', f'{commit_data:,}')

    print('Total GitHub API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items():
        print('{:<28}'.format(' ' + funct_name + ':'), '{:>6}'.format(count))
