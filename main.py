# Credits to https://github.com/Andrew6rant/Andrew6rant/blob/main/today.py

import datetime
from dateutil import relativedelta
import requests
import os
from xml.dom import minidom
import time
import hashlib
import json
from dotenv import load_dotenv
import logging

load_dotenv()

# Logger configuration
LOG_LEVEL = os.getenv(
    "LOG_LEVEL", "INFO"
).upper()  # DEBUG, INFO, WARNING, ERROR, CRITICAL
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# PAT with permissions: read:enterprise, read:org, read:repo_hook, read:user, repo
HEADERS = {"authorization": "token " + os.getenv("ACCESS_TOKEN")}
USER_NAME = os.getenv("USER_NAME")
QUERY_COUNT = {
    "user_getter": 0,
    "follower_getter": 0,
    "graph_repos_stars": 0,
    "recursive_loc": 0,
    "graph_commits": 0,
    "loc_query": 0,
}

# Retry configuration for transient errors
MAX_RETRIES = 3
RETRY_DELAY_BASE = 2  # Base delay in seconds (will be multiplied exponentially)
TRANSIENT_ERROR_CODES = {502, 503, 504}  # Gateway errors worth retrying
MAX_COMMITS_PER_REPO = (
    5000  # Max commits to fetch per repo to avoid excessive API calls
)


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return "{} {}, {} {}, {} {}{}".format(
        diff.years,
        "year" + format_plural(diff.years),
        diff.months,
        "month" + format_plural(diff.months),
        diff.days,
        "day" + format_plural(diff.days),
        " 🎂" if (diff.months == 0 and diff.days == 0) else "",
    )


def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return "s" if unit != 1 else ""


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    logger.debug(f"API request for {func_name} with variables: {variables}")
    request = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables},
        headers=HEADERS,
    )
    if request.status_code == 200:
        logger.debug(f"API request for {func_name} succeeded")
        return request
    logger.error(
        f"{func_name} failed with status {request.status_code}: {request.text}"
    )
    raise Exception(
        func_name, " has failed with a", request.status_code, request.text, QUERY_COUNT
    )


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return my total commit count
    """
    logger.debug(f"Fetching commits from {start_date} to {end_date}")
    query_count("graph_commits")
    query = """
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }"""
    variables = {"start_date": start_date, "end_date": end_date, "login": USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    total = int(
        request.json()["data"]["user"]["contributionsCollection"][
            "contributionCalendar"
        ]["totalContributions"]
    )
    logger.info(f"Total contributions: {total}")
    return total


def graph_repos_stars(count_type, owner_affiliation, cursor=None, add_loc=0, del_loc=0):
    """
    Uses GitHub's GraphQL v4 API to return my total repository, star, or lines of code count.
    """
    logger.debug(f"Fetching {count_type} with affiliation: {owner_affiliation}")
    query_count("graph_repos_stars")
    query = """
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
    }"""
    variables = {
        "owner_affiliation": owner_affiliation,
        "login": USER_NAME,
        "cursor": cursor,
    }
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == "repos":
            result = request.json()["data"]["user"]["repositories"]["totalCount"]
            logger.info(f"Total repositories: {result}")
            return result
        elif count_type == "stars":
            result = stars_counter(
                request.json()["data"]["user"]["repositories"]["edges"]
            )
            logger.info(f"Total stars: {result}")
            return result


def recursive_loc(
    owner,
    repo_name,
    data,
    cache_comment,
    addition_total=0,
    deletion_total=0,
    my_commits=0,
    cursor=None,
    retry_count=0,
    commits_fetched=0,
):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    """
    logger.debug(
        f"Fetching LOC for {owner}/{repo_name}, cursor: {cursor}, fetched: {commits_fetched}"
    )
    query_count("recursive_loc")
    query = """
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
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
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
    }"""
    variables = {"repo_name": repo_name, "owner": owner, "cursor": cursor}
    request = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables},
        headers=HEADERS,
    )  # I cannot use simple_request(), because I want to save the file before raising Exception
    if request.status_code == 200:
        if (
            request.json()["data"]["repository"]["defaultBranchRef"] is not None
        ):  # Only count commits if repo isn't empty
            return loc_counter_one_repo(
                owner,
                repo_name,
                data,
                cache_comment,
                request.json()["data"]["repository"]["defaultBranchRef"]["target"][
                    "history"
                ],
                addition_total,
                deletion_total,
                my_commits,
                commits_fetched,
            )
        else:
            logger.debug(f"Repository {owner}/{repo_name} is empty")
            return 0

    # Handle transient errors with retry
    if request.status_code in TRANSIENT_ERROR_CODES and retry_count < MAX_RETRIES:
        delay = RETRY_DELAY_BASE * (2**retry_count)
        logger.warning(
            f"Transient error {request.status_code} for {owner}/{repo_name}. "
            f"Retrying in {delay}s (attempt {retry_count + 1}/{MAX_RETRIES})"
        )
        time.sleep(delay)
        return recursive_loc(
            owner,
            repo_name,
            data,
            cache_comment,
            addition_total,
            deletion_total,
            my_commits,
            cursor,
            retry_count + 1,
        )

    force_close_file(
        data, cache_comment
    )  # saves what is currently in the file before this program crashes
    if request.status_code == 403:
        logger.error("Rate limit hit! Too many requests in a short amount of time.")
        raise Exception(
            "Too many requests in a short amount of time!\nYou've hit the non-documented anti-abuse limit!"
        )
    logger.error(
        f"recursive_loc failed with status {request.status_code}: {request.text}"
    )
    raise Exception(
        "recursive_loc() has failed with a",
        request.status_code,
        request.text,
        QUERY_COUNT,
    )


def loc_counter_one_repo(
    owner,
    repo_name,
    data,
    cache_comment,
    history,
    addition_total,
    deletion_total,
    my_commits,
    commits_fetched=0,
):
    """
    Recursively call recursive_loc (since GraphQL can only search 100 commits at a time)
    only adds the LOC value of commits authored by me
    """
    commits_fetched += len(history["edges"])

    for node in history["edges"]:
        if node["node"]["author"]["user"] == OWNER_ID:
            my_commits += 1
            addition_total += node["node"]["additions"]
            deletion_total += node["node"]["deletions"]

    # Stop if we've hit the limit or no more pages
    if history["edges"] == [] or not history["pageInfo"]["hasNextPage"]:
        return addition_total, deletion_total, my_commits

    if commits_fetched >= MAX_COMMITS_PER_REPO:
        logger.warning(
            f"Reached max commit limit ({MAX_COMMITS_PER_REPO}) for {owner}/{repo_name}, stopping fetch"
        )
        return addition_total, deletion_total, my_commits

    return recursive_loc(
        owner,
        repo_name,
        data,
        cache_comment,
        addition_total,
        deletion_total,
        my_commits,
        history["pageInfo"]["endCursor"],
        commits_fetched=commits_fetched,
    )


def loc_query(
    owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]
):
    """
    Uses GitHub's GraphQL v4 API to query all the repositories I have access to (with respect to owner_affiliation)
    Queries 60 repos at a time, because larger queries give a 502 timeout error and smaller queries send too many
    requests and also give a 502 error.
    Returns the total number of lines of code in all repositories
    """
    query_count("loc_query")
    query = """
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
    }"""
    variables = {
        "owner_affiliation": owner_affiliation,
        "login": USER_NAME,
        "cursor": cursor,
    }
    request = simple_request(loc_query.__name__, query, variables)
    if request.json()["data"]["user"]["repositories"]["pageInfo"][
        "hasNextPage"
    ]:  # If repository data has another page
        edges += request.json()["data"]["user"]["repositories"][
            "edges"
        ]  # Add on to the LoC count
        return loc_query(
            owner_affiliation,
            comment_size,
            force_cache,
            request.json()["data"]["user"]["repositories"]["pageInfo"]["endCursor"],
            edges,
        )
    else:
        return cache_builder(
            edges + request.json()["data"]["user"]["repositories"]["edges"],
            comment_size,
            force_cache,
        )


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository in edges to see if it has been updated since the last time it was cached
    If it has, run recursive_loc on that repository to update the LOC count
    """
    logger.debug(f"Building cache for {len(edges)} repositories")
    cached = True  # Assume all repositories are cached
    filename = (
        "cache/" + hashlib.sha256(USER_NAME.encode("utf-8")).hexdigest() + ".txt"
    )  # Create a unique filename for each user
    try:
        with open(filename, "r") as f:
            data = f.readlines()
        logger.debug(f"Loaded cache file: {filename}")
    except FileNotFoundError:  # If the cache file doesn't exist, create it
        logger.info(f"Cache file not found, creating: {filename}")
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append(
                    "This line is a comment block. Write whatever you want here.\n"
                )
        with open(filename, "w") as f:
            f.writelines(data)

    if (
        len(data) - comment_size != len(edges) or force_cache
    ):  # If the number of repos has changed, or force_cache is True
        cached = False
        logger.info("Cache invalidated, flushing and rebuilding")
        flush_cache(edges, filename, comment_size)
        with open(filename, "r") as f:
            data = f.readlines()

    cache_comment = data[:comment_size]  # save the comment block
    data = data[comment_size:]  # remove those lines
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if (
            repo_hash
            == hashlib.sha256(
                edges[index]["node"]["nameWithOwner"].encode("utf-8")
            ).hexdigest()
        ):
            try:
                if (
                    int(commit_count)
                    != edges[index]["node"]["defaultBranchRef"]["target"]["history"][
                        "totalCount"
                    ]
                ):
                    # if commit count has changed, update loc for that repo
                    owner, repo_name = edges[index]["node"]["nameWithOwner"].split("/")
                    logger.info(f"Updating LOC for {owner}/{repo_name}")
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = (
                        repo_hash
                        + " "
                        + str(
                            edges[index]["node"]["defaultBranchRef"]["target"][
                                "history"
                            ]["totalCount"]
                        )
                        + " "
                        + str(loc[2])
                        + " "
                        + str(loc[0])
                        + " "
                        + str(loc[1])
                        + "\n"
                    )
            except TypeError:  # If the repo is empty
                logger.debug(f"Repository at index {index} is empty")
                data[index] = repo_hash + " 0 0 0 0\n"
    with open(filename, "w") as f:
        f.writelines(cache_comment)
        f.writelines(data)
    logger.debug(f"Cache saved to {filename}")
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    logger.info(
        f"Total LOC - Added: {loc_add}, Deleted: {loc_del}, Net: {loc_add - loc_del}"
    )
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """
    Wipes the cache file
    This is called when the number of repositories changes or when the file is first created
    """
    with open(filename, "r") as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size]  # only save the comment
    with open(filename, "w") as f:
        f.writelines(data)
        for node in edges:
            f.write(
                hashlib.sha256(
                    node["node"]["nameWithOwner"].encode("utf-8")
                ).hexdigest()
                + " 0 0 0 0\n"
            )


def add_archive():
    """
    Several repositories I have contributed to have since been deleted.
    This function adds them using their last known data
    """
    try:
        logger.debug("Loading archived repository data")
        with open("cache/repository_archive.txt", "r") as f:
            data = f.readlines()
        old_data = data
        data = data[7 : len(data) - 3]  # remove the comment block
        added_loc, deleted_loc = 0, 0
        contributed_repos = len(data)
        for line in data:
            repo_hash, total_commits, my_commits, *loc = line.split()
            added_loc += int(loc[0])
            deleted_loc += int(loc[1])
        my_commits = old_data[-1].split()[4][:-1]
        logger.info(f"Loaded {contributed_repos} archived repositories")
        return [
            added_loc,
            deleted_loc,
            added_loc - deleted_loc,
            my_commits,
            contributed_repos,
        ]
    except Exception as e:
        logger.warning(f"Failed to load archive data: {e}")
        return [0, 0, 0, 0, 0]


def force_close_file(data, cache_comment):
    """
    Forces the file to close, preserving whatever data was written to it
    This is needed because if this function is called, the program would've crashed before the file is properly saved and closed
    """
    filename = "cache/" + hashlib.sha256(USER_NAME.encode("utf-8")).hexdigest() + ".txt"
    with open(filename, "w") as f:
        f.writelines(cache_comment)
        f.writelines(data)
    logger.warning(
        f"Error while writing to cache file. Partial data saved to {filename}"
    )


def stars_counter(data):
    """
    Count total stars in repositories owned by me
    """
    total_stars = 0
    for node in data:
        total_stars += node["node"]["stargazers"]["totalCount"]
    return total_stars


def svg_overwrite(
    filename,
    age_data,
    commit_data,
    star_data,
    repo_data,
    contrib_data,
    follower_data,
    loc_data,
    info,
):
    """
    Parse SVG files and update elements with profile info, age, commits, stars, repositories, and lines written.
    Profile info is loaded from info.json and escaped to prevent SVG rendering issues.
    """
    logger.debug(f"Updating SVG file: {filename}")
    svg = minidom.parse(filename)
    f = open(filename, mode="w", encoding="utf-8")
    tspan = svg.getElementsByTagName("tspan")

    # Update profile info from info.json (with XML escaping)
    profile = info.get("profile", {})
    system = info.get("system", {})
    languages = info.get("languages", {})
    hobbies = info.get("hobbies", {})
    contact = info.get("contact", {})

    # Profile header (index 29): username@hostname
    username = escape_xml(profile.get("username", "user"))
    hostname = escape_xml(profile.get("hostname", "host"))
    tspan[29].firstChild.data = f"{username}@{hostname}"

    # System info
    tspan[32].firstChild.data = escape_xml(system.get("os", "Unknown OS"))
    tspan[34].firstChild.data = age_data  # Uptime (age_data from birthday calc)
    tspan[36].firstChild.data = escape_xml(system.get("host", "Unknown"))
    tspan[38].firstChild.data = escape_xml(system.get("kernel", "Unknown"))
    tspan[40].firstChild.data = escape_xml(system.get("ide", "Unknown IDE"))

    # Languages
    tspan[43].firstChild.data = escape_xml(languages.get("programming", ""))
    tspan[46].firstChild.data = escape_xml(languages.get("computer", ""))
    tspan[49].firstChild.data = escape_xml(languages.get("real", ""))

    # Hobbies
    tspan[52].firstChild.data = escape_xml(hobbies.get("software", ""))
    tspan[55].firstChild.data = escape_xml(hobbies.get("real", ""))

    # Contact
    tspan[59].firstChild.data = escape_xml(contact.get("email", ""))
    tspan[61].firstChild.data = escape_xml(contact.get("linkedin", ""))
    tspan[63].firstChild.data = escape_xml(contact.get("twitter", ""))

    # GitHub Stats (dynamic data)
    tspan[67].firstChild.data = repo_data
    tspan[69].firstChild.data = contrib_data
    tspan[71].firstChild.data = commit_data
    tspan[73].firstChild.data = star_data
    tspan[75].firstChild.data = follower_data
    tspan[77].firstChild.data = loc_data[2]
    tspan[78].firstChild.data = loc_data[0] + "++"
    tspan[79].firstChild.data = loc_data[1] + "--"

    f.write(svg.toxml("utf-8").decode("utf-8"))
    f.close()
    logger.info(f"SVG file updated: {filename}")


def commit_counter(comment_size):
    """
    Counts up my total commits, using the cache file created by cache_builder.
    """
    total_commits = 0
    filename = (
        "cache/" + hashlib.sha256(USER_NAME.encode("utf-8")).hexdigest() + ".txt"
    )  # Use the same filename as cache_builder
    with open(filename, "r") as f:
        data = f.readlines()
    cache_comment = data[:comment_size]  # save the comment block
    data = data[comment_size:]  # remove those lines
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def svg_element_getter(filename):
    """
    Prints the element index of every element in the SVG file
    """
    svg = minidom.parse(filename)
    open(filename, mode="r", encoding="utf-8")
    tspan = svg.getElementsByTagName("tspan")
    for index in range(len(tspan)):
        print(index, tspan[index].firstChild.data)


def user_getter(username):
    """
    Returns the account ID and creation time of the user
    """
    logger.debug(f"Fetching user data for: {username}")
    query_count("user_getter")
    query = """
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }"""
    variables = {"login": username}
    request = simple_request(user_getter.__name__, query, variables)
    user_id = request.json()["data"]["user"]["id"]
    created_at = request.json()["data"]["user"]["createdAt"]
    logger.info(f"User {username} - ID: {user_id}, Created: {created_at}")
    return {"id": user_id}, created_at


def follower_getter(username):
    """
    Returns the number of followers of the user
    """
    logger.debug(f"Fetching follower count for: {username}")
    query_count("follower_getter")
    query = """
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }"""
    request = simple_request(follower_getter.__name__, query, {"login": username})
    count = int(request.json()["data"]["user"]["followers"]["totalCount"])
    logger.info(f"Follower count for {username}: {count}")
    return count


def query_count(funct_id):
    """
    Counts how many times the GitHub GraphQL API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def escape_xml(text):
    """
    Escape special XML characters to prevent SVG rendering issues.
    """
    if text is None:
        return ""
    text = str(text)
    # Replace special characters that could break XML/SVG
    replacements = [
        ("&", "&amp;"),  # Must be first
        ("<", "&lt;"),
        (">", "&gt;"),
        ('"', "&quot;"),
        ("'", "&apos;"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def load_info():
    """
    Load profile information from info.json file.
    Returns dict with profile, system, languages, hobbies, and contact info.
    """
    logger.debug("Loading profile info from info.json")
    try:
        with open("info.json", "r", encoding="utf-8") as f:
            info = json.load(f)
        logger.info("Loaded profile info from info.json")
        return info
    except FileNotFoundError:
        logger.error("info.json not found, using empty defaults")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse info.json: {e}")
        return {}


def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints a formatted time differential
    Returns formatted result if whitespace is specified, otherwise returns raw result
    """
    print("{:<23}".format("   " + query_type + ":"), sep="", end="")
    print("{:>12}".format("%.4f" % difference + " s ")) if difference > 1 else print(
        "{:>12}".format("%.4f" % (difference * 1000) + " ms")
    )
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == "__main__":
    """
    Andrew Grant (Andrew6rant), 2022-2023
    """
    logger.info("Starting GitHub stats calculation")
    print("Calculation times:")

    # Load profile info from info.json
    info = load_info()

    # define global variable for owner ID and calculate user's creation date
    # e.g {'id': 'MDQ6VXNlcjU3MzMxMTM0'} and 2019-11-03T21:15:07Z for username 'Andrew6rant'
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter("account data", user_time)
    # age_data, age_time = perf_counter(daily_readme, datetime.datetime(2006, 1, 18))
    birthday = list(int(x) for x in os.getenv("BIRTHDAY").split("-"))
    age_data, age_time = perf_counter(
        daily_readme, datetime.datetime(birthday[0], birthday[1], birthday[2])
    )
    formatter("age calculation", age_time)
    total_loc, loc_time = perf_counter(
        loc_query, ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"], 7
    )
    formatter("LOC (cached)", loc_time) if total_loc[-1] else formatter(
        "LOC (no cache)", loc_time
    )
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, "stars", ["OWNER"])
    repo_data, repo_time = perf_counter(graph_repos_stars, "repos", ["OWNER"])
    contrib_data, contrib_time = perf_counter(
        graph_repos_stars, "repos", ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"]
    )
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    # several repositories that I've contributed to have since been deleted.
    if OWNER_ID == {"id": os.getenv("NODE_ID")}:  # only calculate for user Andrew6rant
        archived_data = add_archive()
        for index in range(len(total_loc) - 1):
            total_loc[index] += archived_data[index]
        contrib_data += archived_data[-1]
        commit_data += int(archived_data[-2])

    commit_data = formatter("commit counter", commit_time, commit_data, 7)
    star_data = formatter("star counter", star_time, star_data)
    repo_data = formatter("my repositories", repo_time, repo_data, 2)
    contrib_data = formatter("contributed repos", contrib_time, contrib_data, 2)
    follower_data = formatter("follower counter", follower_time, follower_data, 4)

    for index in range(len(total_loc) - 1):
        total_loc[index] = "{:,}".format(
            total_loc[index]
        )  # format added, deleted, and total LOC

    svg_overwrite(
        "dark_mode.svg",
        age_data,
        commit_data,
        star_data,
        repo_data,
        contrib_data,
        follower_data,
        total_loc[:-1],
        info,
    )
    svg_overwrite(
        "light_mode.svg",
        age_data,
        commit_data,
        star_data,
        repo_data,
        contrib_data,
        follower_data,
        total_loc[:-1],
        info,
    )

    # move cursor to override 'Calculation times:' with 'Total function time:' and the total function time, then move cursor back
    print(
        "\033[F\033[F\033[F\033[F\033[F\033[F\033[F\033[F",
        "{:<21}".format("Total function time:"),
        "{:>11}".format(
            "%.4f"
            % (
                user_time
                + age_time
                + loc_time
                + commit_time
                + star_time
                + repo_time
                + contrib_time
            )
        ),
        " s \033[E\033[E\033[E\033[E\033[E\033[E\033[E\033[E",
        sep="",
    )

    total_calls = sum(QUERY_COUNT.values())
    print("Total GitHub GraphQL API calls:", "{:>3}".format(total_calls))
    for funct_name, count in QUERY_COUNT.items():
        print("{:<28}".format("   " + funct_name + ":"), "{:>6}".format(count))

    logger.info(f"Completed - Total API calls: {total_calls}")
    logger.debug(f"API call breakdown: {QUERY_COUNT}")
